#!/bin/sh
# Exits 0 (a "success" as far as the OS is concerned) but with no
# stdout at all -- malformed output that a naive "exit code 0 means
# fine" check would miss.
exit 0
