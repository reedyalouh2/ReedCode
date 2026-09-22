import json
import sys
from collections import Counter

path = sys.argv[1]

commands = 0
failed_commands = 0
file_changes = 0
agent_messages = 0
command_output_bytes = 0

command_types = Counter()

# Final cumulative usage
input_tokens = 0
cached_input_tokens = 0
output_tokens = 0
reasoning_output_tokens = 0

duration_ms = 0
time_to_first_token_ms = 0


with open(path) as f:
    for line in f:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        event_type = event.get("type")
        payload = event.get("payload", {})

        # -------------------------
        # COMPLETED AGENT ITEMS
        # -------------------------
        if (
            event_type == "event_msg"
            and payload.get("type") == "item_completed"
        ):
            item = payload.get("item", {})
            item_type = item.get("type", "")

            if item_type == "CommandExecution":
                commands += 1

                command = item.get("command", "")

                if isinstance(command, list):
                    command_text = " ".join(command)
                else:
                    command_text = command

                if command_text:
                    command_types[command_text.split()[0]] += 1

                output = item.get("aggregated_output", "")
                command_output_bytes += len(output.encode("utf-8"))

                exit_code = item.get("exit_code")
                if exit_code not in (0, None):
                    failed_commands += 1

            elif item_type == "FileChange":
                file_changes += 1

            elif item_type == "AgentMessage":
                agent_messages += 1

        # -------------------------
        # TOKEN USAGE
        # -------------------------
        elif event_type == "token_usage_record":
            # This is cumulative for the entire thread.
            usage = payload.get("thread_token_usage", {})

            input_tokens = usage.get("input_tokens", input_tokens)
            cached_input_tokens = usage.get(
                "cached_input_tokens",
                cached_input_tokens,
            )
            output_tokens = usage.get(
                "output_tokens",
                output_tokens,
            )
            reasoning_output_tokens = usage.get(
                "reasoning_output_tokens",
                reasoning_output_tokens,
            )

        # -------------------------
        # TASK COMPLETE
        # -------------------------
        elif (
            event_type == "event_msg"
            and payload.get("type") == "task_complete"
        ):
            duration_ms = payload.get("duration_ms", 0)
            time_to_first_token_ms = payload.get(
                "time_to_first_token_ms",
                0,
            )


cache_rate = (
    cached_input_tokens / input_tokens * 100
    if input_tokens
    else 0
)

fresh_input_tokens = input_tokens - cached_input_tokens


print("\n=== CODEX WORKLOAD PROFILE ===")

print(f"\nCommands executed:       {commands}")
print(f"Failed commands:         {failed_commands}")
print(f"File changes:            {file_changes}")
print(f"Agent messages:          {agent_messages}")

print(f"\nCommand output bytes:    {command_output_bytes:,}")

print(f"\nInput tokens:            {input_tokens:,}")
print(f"Cached input tokens:     {cached_input_tokens:,}")
print(f"Fresh input tokens:      {fresh_input_tokens:,}")
print(f"Cache reuse rate:        {cache_rate:.2f}%")
print(f"Output tokens:           {output_tokens:,}")
print(f"Reasoning output tokens: {reasoning_output_tokens:,}")

print(f"\nTask duration:           {duration_ms / 1000:.2f}s")
print(
    f"Time to first token:     "
    f"{time_to_first_token_ms / 1000:.2f}s"
)

print("\nCommand types:")
for command, count in command_types.most_common():
    print(f"  {command:<20} {count}")