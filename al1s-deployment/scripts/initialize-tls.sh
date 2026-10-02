#!/usr/bin/env sh
set -eu

if [ "$#" -lt 1 ]; then
  echo "Usage: $0 <dns-name> [--ip <ip-address>] [--output <output-directory>]" >&2
  exit 2
fi

dns_name="$1"
shift
deployment_root="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
output_directory="$deployment_root/compose/tls/generated"
ip_address=""

while [ "$#" -gt 0 ]; do
  case "$1" in
    --ip)
      [ "$#" -ge 2 ] || { echo "--ip requires an address" >&2; exit 2; }
      ip_address="$2"
      shift 2
      ;;
    --output)
      [ "$#" -ge 2 ] || { echo "--output requires a directory" >&2; exit 2; }
      output_directory="$2"
      shift 2
      ;;
    *)
      echo "Unknown argument: $1" >&2
      echo "Usage: $0 <dns-name> [--ip <ip-address>] [--output <output-directory>]" >&2
      exit 2
      ;;
  esac
done

for name in ca.crt ca.key ca.srl server.crt server.key; do
  if [ -e "$output_directory/$name" ]; then
    echo "TLS assets already exist in $output_directory; refusing to replace the CA." >&2
    exit 1
  fi
done

mkdir -p "$output_directory"
extension_file="$output_directory/server-ext.cnf"
trap 'rm -f "$extension_file" "$output_directory/server.csr"' EXIT
subject_alt_name="DNS:$dns_name"
if [ -n "$ip_address" ]; then
  subject_alt_name="$subject_alt_name,IP:$ip_address"
fi
cat > "$extension_file" <<EOF
subjectAltName=$subject_alt_name
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid,issuer
EOF

openssl genrsa -out "$output_directory/ca.key" 3072
MSYS_NO_PATHCONV=1 openssl req -x509 -new -sha256 -key "$output_directory/ca.key" -out "$output_directory/ca.crt" -days 3650 -subj "/CN=AL-1S Private CA" -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" -addext "subjectKeyIdentifier=hash"
openssl genrsa -out "$output_directory/server.key" 3072
MSYS_NO_PATHCONV=1 openssl req -new -sha256 -key "$output_directory/server.key" -out "$output_directory/server.csr" -subj "/CN=$dns_name"
openssl x509 -req -sha256 -in "$output_directory/server.csr" -CA "$output_directory/ca.crt" -CAkey "$output_directory/ca.key" -CAcreateserial -out "$output_directory/server.crt" -days 825 -extfile "$extension_file"
openssl verify -x509_strict -purpose sslserver -CAfile "$output_directory/ca.crt" "$output_directory/server.crt"
chmod 0600 "$output_directory/ca.key" "$output_directory/server.key"
chmod 0644 "$output_directory/ca.crt" "$output_directory/server.crt"

echo "TLS assets created in $output_directory"
echo "Certificate SAN: $subject_alt_name"
echo "Install ca.crt on terminals; never copy ca.key to a terminal or container."
