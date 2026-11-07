#!/bin/sh
cat > /dev/null
case ":$PATH:" in *toolbin*) touch tool_marker.txt ;; esac
exit 0
