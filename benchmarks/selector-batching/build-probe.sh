#!/bin/bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
build="$root/build"
mkdir -p "$build"
read -r -a includes <<< "$(pkg-config --cflags Qt6Quick)"
"$(qmake6 -query QT_HOST_LIBEXECS)/moc" "${includes[@]}" "$root/probe.cpp" -o "$build/probe.moc"
read -r -a flags <<< "$(pkg-config --cflags --libs Qt6Quick)"
c++ -std=c++17 -O2 -Wall -Wextra -Werror -fPIC -shared -I"$build" \
  "$root/probe.cpp" "${flags[@]}" -o "$build/libbenchmarkprobe.so"
