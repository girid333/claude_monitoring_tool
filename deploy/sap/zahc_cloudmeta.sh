#!/bin/sh
# ZAHC_CLOUDMETA — read-only cloud placement facts for Sentinel's Performance view (D-052b).
#
# Prints key=value lines (each < 128 chars, the SXPG line limit):
#   provider region zone vm_size placement_group instance_id   (or: error=...)
# Only reads the cloud provider's link-local instance metadata service; changes nothing.
#
# Install on EVERY SAP application server host (and on the HANA host if it runs
# an SAP gateway), e.g. as /usr/sap/scripts/zahc_cloudmeta.sh, owner root,
# mode 0755. Then in SM69 create, once per system:
#   Command name ZAHC_CLOUDMETA · Operating system Linux
#   OS command   /usr/sap/scripts/zahc_cloudmeta.sh
#   Parameters   (empty) · "Additional parameters allowed" NOT set
# Sentinel runs it per host with SXPG_COMMAND_EXECUTE (TARGETSYSTEM = host),
# so the monitoring user needs S_LOG_COM for ZAHC_CLOUDMETA and S_RZL_ADM 03.
#
# Azure note: the metadata service does not expose a proximity placement
# group, so on Azure only region, zone and VM size are reported.

T=2                                   # seconds per request — the service is link-local
M=http://169.254.169.254
get() { curl -sf -m "$T" "$@" 2>/dev/null; }
say() { [ -n "$2" ] && printf '%s=%s\n' "$1" "$(printf '%s' "$2" | tr -d '\r\n' | cut -c1-100)"; }

command -v curl >/dev/null 2>&1 || { echo "error=curl not installed"; exit 0; }

# AWS (IMDSv2 first, IMDSv1 fallback)
TOK=$(get -X PUT "$M/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 60")
aws() { if [ -n "$TOK" ]; then get -H "X-aws-ec2-metadata-token: $TOK" "$M/latest/meta-data/$1"
        else get "$M/latest/meta-data/$1"; fi; }
AZ=$(aws placement/availability-zone)
if [ -n "$AZ" ]; then
  say provider AWS
  say region "$(aws placement/region)"
  say zone "$AZ"
  say vm_size "$(aws instance-type)"
  say placement_group "$(aws placement/group-name)"
  say instance_id "$(aws instance-id)"
  exit 0
fi

# Azure
az() { get -H "Metadata:true" "$M/metadata/instance/compute/$1?api-version=2021-02-01&format=text"; }
LOC=$(az location)
if [ -n "$LOC" ]; then
  say provider Azure
  say region "$LOC"
  ZN=$(az zone); say zone "${ZN:+$LOC-$ZN}"
  say vm_size "$(az vmSize)"
  say placement_group "$(az placementGroupId)"
  say instance_id "$(az vmId)"
  exit 0
fi

# Google Cloud
gcp() { get -H "Metadata-Flavor: Google" "http://metadata.google.internal/computeMetadata/v1/instance/$1"; }
GZ=$(gcp zone)
if [ -n "$GZ" ]; then
  Z=${GZ##*/}
  say provider "Google Cloud"
  say region "${Z%-*}"
  say zone "$Z"
  MT=$(gcp machine-type); say vm_size "${MT##*/}"
  say instance_id "$(gcp id)"
  exit 0
fi

echo "provider=none"
echo "error=no cloud metadata service on $(hostname) (on-premises or blocked)"
exit 0
