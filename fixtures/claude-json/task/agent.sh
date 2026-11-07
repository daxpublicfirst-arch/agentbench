#!/bin/sh
cat > /dev/null
printf '%s\n' '{"type": "result", "subtype": "success", "result": "done", "session_id": "550e8400-e29b-41d4-a716-446655440000", "usage": {"input_tokens": 100, "output_tokens": 50, "cache_creation_input_tokens": 200, "cache_read_input_tokens": 500}, "modelUsage": {"claude-sonnet-4-6": {"inputTokens": 100, "outputTokens": 50, "cacheCreationInputTokens": 200, "cacheReadInputTokens": 500}}, "total_cost_usd": 0.01, "num_turns": 2, "duration_ms": 1234}'
exit 0
