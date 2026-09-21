#!/usr/bin/env bash
# Paulo Aleixo Campos
__dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
function shw_info { echo -e '\033[1;34m'"$1"'\033[0m'; }
function error { echo "ERROR in ${1}"; exit 99; }
trap 'error $LINENO' ERR
PS4='████████████████████████${BASH_SOURCE}@${FUNCNAME[0]:-}[${LINENO}]>  '
set -o errexit
set -o pipefail
set -o nounset
#set -o xtrace


cd "${__dir}"
APPNAME=$(basename $PWD)
# Build identity (see version/version.go). Commit is stamped from git;
# VERSION=x.y.z ./go_build.sh overrides the version baked into the source.
COMMIT=$(git rev-parse --short HEAD 2>/dev/null || echo none)
LDFLAGS="-s -w -extldflags \"-static\" -X kidsout/version.Commit=${COMMIT}"
[[ -n "${VERSION:-}" ]] && LDFLAGS="${LDFLAGS} -X kidsout/version.Version=${VERSION}"
CGO_ENABLED=0 GOOS=linux go build -a -trimpath -ldflags "${LDFLAGS}" -o ${APPNAME} .
./${APPNAME} --version
#GOOS=windows go build . -o ${APPNAME}.exe

ls -lrth
