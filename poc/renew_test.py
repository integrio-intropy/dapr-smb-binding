import time, smbclient, subprocess, os
S="dc1.lab.test"; P=r"\\dc1.lab.test\exchange\in"
cache={}
def connect():
    smbclient.register_session(S, auth_protocol="kerberos", encrypt=True, connection_cache=cache)
connect()
print("t=0   connected, listdir:", len(smbclient.listdir(P, connection_cache=cache)), "entries")
time.sleep(200)
try:
    print("t=200 SAME session after ticket expiry, listdir:", len(smbclient.listdir(P, connection_cache=cache)), "entries")
except Exception as e:
    print("t=200 same session failed:", type(e).__name__, str(e)[:150])
try:
    smbclient.reset_connection_cache(connection_cache=cache)
except Exception as e:
    print('t=200 reset of dead session raised (ignored):', type(e).__name__)
cache.clear()
connect()
print("t=200 NEW session after expiry (keytab re-acquire), listdir:", len(smbclient.listdir(P, connection_cache=cache)), "entries")
