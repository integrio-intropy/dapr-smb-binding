# What this changes

<!-- One or two sentences. Link the Linear issue if there is one. -->

# Why

<!-- What was wrong, or what could not be done before. -->

# Checklist

- [ ] `make lint test` passes.
- [ ] `make integration` passes (say below if you also ran `SMB_IT_EXPIRY=1`).
- [ ] There is a test that fails without this change.
- [ ] README, CONTRACT.md and `metadata.yaml` are updated if behaviour or
      metadata changed. Contract changes must be mirrored in the FTP and SFTP
      bindings.

# Anything reviewers should look at closely

<!-- Anything touching auth: confirm NTLM is still never a fallback. -->
