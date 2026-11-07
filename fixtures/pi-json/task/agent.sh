#!/bin/sh
cat > /dev/null
printf '%s\n' '{"type": "session", "session_id": "pi-1"}'
printf '%s\n' '{"type": "message_end", "message": {"role": "assistant", "text": "done"}, "usage": {"input": 40, "output": 10, "cacheRead": 20, "cacheWrite": 5}}'
printf '%s\n' '{"type": "message_end", "message": {"role": "assistant", "text": "done"}, "usage": {"input": 60, "output": 20, "cacheRead": 0, "cacheWrite": 10}}'
printf '%s\n' '{"type": "agent_end"}'
exit 0
