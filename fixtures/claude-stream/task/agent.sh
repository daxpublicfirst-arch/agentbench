#!/bin/sh
cat > /dev/null
printf '%s\n' '{"type": "system", "subtype": "init", "session_id": "550e8400-e29b-41d4-a716-446655440000"}'
printf '%s\n' '{"type": "assistant", "message": {"role": "assistant", "content": [], "usage": {"input_tokens": 10, "output_tokens": 5}}}'
printf '%s\n' '{"type": "result", "subtype": "success", "result": "done", "session_id": "550e8400-e29b-41d4-a716-446655440000", "usage": {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 500}}'
exit 0
