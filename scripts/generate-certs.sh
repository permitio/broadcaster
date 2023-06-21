#!/bin/bash
# Setup params
PASSWORD=guessme
VALIDITY=365
PROJECT_PREFIX=broadcaster
BROKERS='localhost'
CLIENT_ALIAS=myclientname
CLIENT_KEYSTORE=$PROJECT_PREFIX.client.keystore.jks
CLIENT_CERT_FILE=$PROJECT_PREFIX-client-cert-file.txt
CLIENT_CERT_SIGNED=$PROJECT_PREFIX-client-cert-signed.crt
CA_ROOT_ALIAS=ca-root
CA_CERT_NAME=ca-cert.crt
CA_KEY=ca-key.key
BROKER_TRUSTSTORE=$PROJECT_PREFIX.truststore.jks
echo -e "OpenSSL based Keys/Cert generation for Kafka"

rm $CLIENT_KEYSTORE
rm $BROKER_TRUSTSTORE

# Generate CA
openssl req -new -x509 -keyout ca-key.key -out ca-cert.crt -days 365 -passin pass:$PASSWORD -subj "/CN=ca-root/OU=SomeUnit/O=SomeOrg/L=London/S=England/C=GB" -passout pass:$PASSWORD

# Generate for all brokers
echo -e "\n\n###\n###Generating Keys for listed brokers = $BROKERS\n\n###\n###"
for BROKER in $BROKERS
do
keytool -genkeypair -keysize 2048 -keyalg RSA -keystore $PROJECT_PREFIX-$BROKER.jks -alias $BROKER -dname "CN=$BROKER,OU=SomeUnit,O=SomeOrg,L=London,S=England,C=GB" -ext SAN=DNS:$BROKER -validity $VALIDITY -keypass $PASSWORD -storepass $PASSWORD
echo -e "subjectAltName=DNS:$BROKER" > $PROJECT_PREFIX-x509v3-$BROKER.ext
done
echo -e "\n\n###\n###Signing and importing certificates using CA file $CA_CERT_NAME and CA keys file $CA_KEY\n\n###\n###"
for BROKER in $BROKERS
do
keytool -certreq -keystore $PROJECT_PREFIX-$BROKER.jks -alias $BROKER -ext SAN=DNS:$BROKER -file $PROJECT_PREFIX-$BROKER-cert-file.txt -storepass $PASSWORD -keypass $PASSWORD
openssl x509 -req -CA $CA_CERT_NAME -CAkey $CA_KEY -in $PROJECT_PREFIX-$BROKER-cert-file.txt -out $PROJECT_PREFIX-$BROKER-cert-signed.crt -days $VALIDITY -CAcreateserial -passin pass:$PASSWORD -extfile $PROJECT_PREFIX-x509v3-$BROKER.ext
done
echo -e "\n\n###\n###Importing CA root $CA_CERT_NAME and signed broker certs into keystoere\n\n###\n###"
for BROKER in $BROKERS
do
keytool -import -keystore $PROJECT_PREFIX-$BROKER.jks -alias $CA_ROOT_ALIAS -file $CA_CERT_NAME -storepass $PASSWORD -keypass $PASSWORD -noprompt
keytool -import -keystore $PROJECT_PREFIX-$BROKER.jks -alias $BROKER -file $PROJECT_PREFIX-$BROKER-cert-signed.crt -storepass $PASSWORD -keypass $PASSWORD -noprompt
done
echo -e "\n\n###\n###Preparing Client Certificates and keystores###\n\n###"
keytool -genkeypair -keysize 2048 -keyalg RSA -keystore $CLIENT_KEYSTORE -alias $CLIENT_ALIAS -dname "CN=$CLIENT_ALIAS,OU=SomeUnit,O=SomeOrg,L=London,S=England,C=GB" -validity $VALIDITY -storepass $PASSWORD -keypass $PASSWORD
keytool -certreq -keystore $CLIENT_KEYSTORE -alias $CLIENT_ALIAS -file $CLIENT_CERT_FILE -storepass $PASSWORD -keypass $PASSWORD
openssl x509 -req -CA $CA_CERT_NAME -CAkey $CA_KEY -in $CLIENT_CERT_FILE -out $CLIENT_CERT_SIGNED -days $VALIDITY -CAcreateserial -passin pass:$PASSWORD
keytool -import -keystore $CLIENT_KEYSTORE -alias $CA_ROOT_ALIAS -file $CA_CERT_NAME -storepass $PASSWORD -keypass $PASSWORD -noprompt
keytool -import -keystore $CLIENT_KEYSTORE -alias $CLIENT_ALIAS -file $CLIENT_CERT_SIGNED -storepass $PASSWORD -keypass $PASSWORD -noprompt
###
# Once everything is done - import CA into broker and client trust stores correctly

#write password to password.txt for docker-compose kafka secrets
echo $PASSWORD > password.txt