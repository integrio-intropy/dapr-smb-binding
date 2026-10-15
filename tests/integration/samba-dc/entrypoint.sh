#!/bin/sh
# Provision LAB.TEST on first start, then run samba in the foreground.
set -eu

REALM=LAB.TEST
DOMAIN=LAB
ADMIN_PASS='Adm1n!Passw0rd'
SVC_PASS='Svc!Passw0rd1'
PARTNER_PASS='Partner!Passw0rd1'
SHARED=/shared

if [ ! -f /var/lib/samba/private/sam.ldb ]; then
  rm -f /etc/samba/smb.conf /etc/krb5.conf
  samba-tool domain provision \
    --realm="$REALM" --domain="$DOMAIN" --server-role=dc \
    --dns-backend=SAMBA_INTERNAL --adminpass="$ADMIN_PASS" \
    --host-name=dc --use-rfc2307 \
    --option="server smb encrypt = required" \
    --option="server min protocol = SMB3"

  # provision drops "ntlm auth" from --option, so set it afterwards. Like a
  # domain that blocks NTLM: the binding must get in with Kerberos only.
  sed -i 's/^\[global\]$/[global]\n\tntlm auth = disabled/' /etc/samba/smb.conf

  mkdir -p /srv/exchange
  chmod 0777 /srv/exchange
  cat >> /etc/samba/smb.conf <<CONF

[exchange]
    path = /srv/exchange
    read only = no
    smb encrypt = required
    # Both svc-intropy and partner read and write everywhere on the share.
    inherit permissions = yes
    force create mode = 0666
    force directory mode = 0777
CONF

  samba-tool user create svc-intropy "$SVC_PASS"
  samba-tool user setexpiry svc-intropy --noexpiry
  samba-tool user create partner "$PARTNER_PASS"
  samba-tool user setexpiry partner --noexpiry

  mkdir -p "$SHARED"
  samba-tool domain exportkeytab "$SHARED/svc-intropy.keytab" --principal=svc-intropy@"$REALM"
  chmod 0644 "$SHARED/svc-intropy.keytab"
  cp /var/lib/samba/private/krb5.conf /etc/krb5.conf
fi

touch "$SHARED/ready"
exec samba --foreground --no-process-group --debug-stdout
