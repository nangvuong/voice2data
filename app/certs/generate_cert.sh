#!/bin/bash
# Script tự động lấy IP hiện tại và tạo chứng chỉ SSL SAN
cd "$(dirname "$0")"

MY_IP=$(hostname -I | awk '{print $1}')
if [ -z "$MY_IP" ]; then
    MY_IP="192.168.40.18"
fi

echo "Tạo chứng chỉ SSL cho IP: $MY_IP"

cat << CONF > openssl.cnf
[req]
default_bits = 2048
prompt = no
default_md = sha256
distinguished_name = dn
x509_extensions = v3_req

[dn]
C = VN
ST = Hanoi
L = Hanoi
O = NongKy AI
OU = AgriTech
CN = $MY_IP

[v3_req]
subjectAltName = @alt_names
basicConstraints = CA:FALSE
keyUsage = nonRepudiation, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth

[alt_names]
IP.1 = $MY_IP
IP.2 = 192.168.40.5
IP.3 = 192.168.40.18
IP.4 = 127.0.0.1
DNS.1 = localhost
DNS.2 = nongky.local
CONF

openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
  -keyout key.pem \
  -out cert.pem \
  -config openssl.cnf -extensions v3_req

echo "Đã tạo cert.pem và key.pem thành công!"
