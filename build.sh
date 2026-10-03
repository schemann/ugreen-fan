#!/bin/bash
# Build the it87 module for the running TrueNAS kernel inside a Debian container.
# Run on the NAS as root after every TrueNAS update.
# No bind mounts: headers go in and the module comes out through the docker stream,
# so DOCKER_HOST may point at another machine (DOCKER_HOST=ssh://user@buildhost).
set -euo pipefail

IT87_REPO=${IT87_REPO:-https://github.com/frankcrawford/it87.git}
IT87_COMMIT=${IT87_COMMIT:-bc06d3488439e5fcd725c1bdcfcac994d6d95cac}

repo=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
release=$(uname -r)
headers=$(readlink -f "/lib/modules/$release/build")
gcc_major=$(sed -n 's/.*gcc (Debian \([0-9]\+\)\..*/\1/p' /proc/version)

case $gcc_major in
    12) image=debian:bookworm ;;
    14) image=debian:trixie ;;
    *) echo "Unsupported kernel compiler 'gcc $gcc_major' in /proc/version" >&2; exit 1 ;;
esac

[[ -d $headers ]] || { echo "Kernel headers not found at $headers" >&2; exit 1; }
[[ $headers == /usr/src/* ]] || { echo "Expected headers under /usr/src, got $headers" >&2; exit 1; }

out="$repo/modules/$release"
mkdir -p "$repo/modules"
tmp=$(mktemp "$repo/modules/.it87.ko.XXXXXX")
trap 'rm -f "$tmp"' EXIT
echo "Building it87 $IT87_COMMIT for $release with gcc-$gcc_major in $image"

# /usr/src goes in as a gzip tar on stdin; it87.ko comes back on stdout, everything else on stderr
tar -C / -czf - usr/src | docker run --rm -i \
    -e GCC="$gcc_major" -e HEADERS="$headers" -e RELEASE="$release" \
    -e IT87_REPO="$IT87_REPO" -e IT87_COMMIT="$IT87_COMMIT" \
    "$image" bash -euo pipefail -c '
        exec 3>&1 1>&2
        tar -C / -xzf -
        apt-get update -qq
        apt-get install -y -qq --no-install-recommends \
            "gcc-$GCC" make git ca-certificates libelf-dev bc kmod >/dev/null </dev/null
        ln -sf "/usr/bin/gcc-$GCC" /usr/bin/gcc
        git clone -q "$IT87_REPO" /src
        git -C /src checkout -q "$IT87_COMMIT"
        make -C "$HEADERS" M=/src CC="gcc-$GCC" modules
        vermagic=$(modinfo -F vermagic /src/it87.ko)
        [[ $vermagic == "$RELEASE "* ]] || { echo "vermagic mismatch: $vermagic" >&2; exit 1; }
        cat /src/it87.ko >&3
    ' >"$tmp"

vermagic=$(modinfo -F vermagic "$tmp")
[[ $vermagic == "$release "* ]] || { echo "vermagic mismatch on the host: $vermagic" >&2; exit 1; }
chmod 644 "$tmp"
mkdir -p "$out"
mv "$tmp" "$out/it87.ko"
echo "Built $out/it87.ko"
