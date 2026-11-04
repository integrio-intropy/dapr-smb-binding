// The soak workload: a file binding driven the way Intropy components drive it, through
// Intropy.Framework's adapters: SmbAdapter for the SMB binding, SftpAdapter for the FTP
// binding (SOAK_KIND=ftp), as components use it. Two modes:
//
//   job      one run to completion under JobRunner, as a CronJob's pod runs (exit 0/1/2)
//   service  an endless loop, as a Deployment's pod runs
//
// Every file written is read back and compared by SHA-256, so a corrupt or lost file is a
// failure, not just an error. Each round prints one SOAK line of JSON for the collector.
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text.Json;
using Dapr.Client;
using Grpc.Net.Client;
using Intropy.Framework.Adapters.File;
using Intropy.Framework.Hosting.Jobs;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Logging;

var mode = args.FirstOrDefault() ?? "job";
var binding = Env("SOAK_BINDING", "soak-share");
var pod = Env("POD_NAME", Environment.MachineName);

var services = new ServiceCollection();
services.AddLogging(b => b.AddSimpleConsole(o => o.SingleLine = true));
services.AddSingleton(_ => new DaprClientBuilder()
    .UseGrpcChannelOptions(new GrpcChannelOptions
    {
        MaxReceiveMessageSize = int.Parse(Env("Dapr__MaxReceiveMessageBytes", "17825792")),
        ThrowOperationCanceledOnCancellation = true,
    })
    .Build());
var options = new FileAdapterOptions(binding, $"soak/{mode}/{pod}");
services.AddSingleton<IFileAdapter>(sp => Env("SOAK_KIND", "smb") == "ftp"
    ? new SftpAdapter(sp.GetRequiredService<DaprClient>(), options)
    : new SmbAdapter(sp.GetRequiredService<DaprClient>(), options));
services.AddSingleton<Round>();

// As the components do: SIGTERM and Ctrl+C cancel the run instead of killing the process.
using var cts = new CancellationTokenSource();
using var sigterm = PosixSignalRegistration.Create(PosixSignal.SIGTERM, context =>
{
    context.Cancel = true;
    cts.Cancel();
});
Console.CancelKeyPress += (_, e) => { e.Cancel = true; cts.Cancel(); };

if (mode == "service")
{
    services.AddSingleton<ServiceLoop>();
    await using var sp = services.BuildServiceProvider();
    await sp.GetRequiredService<ServiceLoop>().RunAsync(cts.Token);
    return 0;
}

services.AddJob<SoakJob>(o =>
{
    o.JobName = "smb-soak";
    o.SidecarTimeout = TimeSpan.FromSeconds(30);
});
await using (var sp = services.BuildServiceProvider())
    return await sp.GetRequiredService<JobRunner>().RunAsync(cts.Token);

static string Env(string name, string fallback) =>
    Environment.GetEnvironmentVariable(name) is { Length: > 0 } v ? v : fallback;

/// <summary>One round: write files of mixed sizes, list them, read each back and compare,
/// delete them. Returns the outcome rather than throwing, so both modes can report it.</summary>
sealed class Round(IFileAdapter adapter)
{
    private static readonly int[] Sizes = Environment.GetEnvironmentVariable("SOAK_SIZES") is { Length: > 0 } s
        ? s.Split(',').Select(int.Parse).ToArray()
        : [1_024, 64_000, 900_000, 4_500_000];

    public async Task<RoundResult> RunAsync(CancellationToken ct)
    {
        var sw = Stopwatch.StartNew();
        var written = new Dictionary<string, byte[]>();
        int ok = 0, mismatch = 0;
        string? error = null;
        string stage = "write";
        try
        {
            // Now and then a file at the 16 MiB limit, the size prod allows.
            var sizes = Random.Shared.Next(20) == 0 ? [.. Sizes, 16 * 1024 * 1024] : Sizes;
            foreach (var size in sizes)
            {
                var name = $"{Guid.NewGuid():N}.bin";
                var content = new byte[size];
                Random.Shared.NextBytes(content);
                await adapter.WriteAsync(name, content, ct: ct);
                written[name] = SHA256.HashData(content);
            }

            stage = "list";
            var listed = (await adapter.ListAsync(ct)).Select(f => f.Name).ToHashSet();
            var missing = written.Keys.Count(n => !listed.Contains(n));
            if (missing > 0)
                throw new InvalidOperationException($"{missing} written file(s) missing from the listing");

            stage = "get";
            foreach (var (name, hash) in written)
            {
                var back = await adapter.GetContentAsync(name, ct);
                if (SHA256.HashData(back).AsSpan().SequenceEqual(hash)) ok++;
                else mismatch++;
            }

            stage = "delete";
            foreach (var name in written.Keys)
                await adapter.DeleteAsync(name, ct);
            written.Clear();
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested)
        {
            throw;
        }
        catch (Exception e)
        {
            error = $"{stage}: {Innermost(e).Message}";
        }

        return new RoundResult(ok, mismatch, error, sw.ElapsedMilliseconds, written.Count);
    }

    private static Exception Innermost(Exception e) => e.InnerException is { } inner ? Innermost(inner) : e;
}

sealed record RoundResult(int Ok, int Mismatch, string? Error, long Ms, int LeftBehind)
{
    public bool Failed => Error is not null || Mismatch > 0;

    public void Print(string mode, string pod) =>
        Console.WriteLine("SOAK " + JsonSerializer.Serialize(new
        {
            ts = DateTimeOffset.UtcNow, mode, pod, ok = Ok, mismatch = Mismatch, error = Error, ms = Ms,
            left = LeftBehind,
        }));
}

/// <summary>The CronJob workload. With SOAK_FAIL_FIRST=1 the first attempt in a pod fails
/// after its round (exit 1), so the soak exercises the same-pod retry that
/// INTROPY_JOB_SIDECAR_SHUTDOWN=OnSuccess exists for. The marker lives on an emptyDir, which a
/// container restart keeps.</summary>
sealed class SoakJob(Round round) : IJob
{
    public async Task<RunSummary> ExecuteAsync(CancellationToken ct)
    {
        var result = await round.RunAsync(ct);
        result.Print("job", Environment.GetEnvironmentVariable("POD_NAME") ?? "");
        if (result.Error is not null)
            throw new InvalidOperationException(result.Error);

        const string marker = "/state/failed-once";
        if (Environment.GetEnvironmentVariable("SOAK_FAIL_FIRST") == "1" && !File.Exists(marker))
        {
            await File.WriteAllTextAsync(marker, DateTimeOffset.UtcNow.ToString("O"), ct);
            throw new InvalidOperationException("injected failure on the first attempt");
        }

        return new RunSummary(result.Ok, result.Mismatch, 0);
    }
}

/// <summary>The Deployment workload: rounds back to back, never giving up, as a loader that
/// must keep delivering whatever happens to its sidecar or the file server.</summary>
sealed class ServiceLoop(Round round, DaprClient dapr, ILogger<ServiceLoop> logger)
{
    public async Task RunAsync(CancellationToken ct)
    {
        var pod = Environment.GetEnvironmentVariable("POD_NAME") ?? "";
        var pause = TimeSpan.FromMilliseconds(int.Parse(Environment.GetEnvironmentVariable("SOAK_PAUSE_MS") ?? "1000"));
        logger.LogInformation("waiting for the sidecar");
        await dapr.WaitForSidecarAsync(ct);
        while (!ct.IsCancellationRequested)
        {
            RoundResult result;
            try { result = await round.RunAsync(ct); }
            catch (OperationCanceledException) when (ct.IsCancellationRequested) { break; }
            result.Print("service", pod);
            try { await Task.Delay(result.Failed ? TimeSpan.FromSeconds(2) : pause, ct); }
            catch (OperationCanceledException) { break; }
        }
    }
}
