# A throwaway Samba AD DC that is also the file server, configured the way a
# locked-down Windows domain is: NTLM refused, SMB 3 with encryption required.
# The expiry test asks for short tickets from the client side (Samba's
# kdc:* ticket lifetime options only take whole hours).
FROM debian:trixie-slim

RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      samba samba-ad-dc samba-ad-provision winbind krb5-user ldb-tools \
 && rm -rf /var/lib/apt/lists/*

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 88 445
ENTRYPOINT ["/entrypoint.sh"]
