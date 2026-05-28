from decompute_ai import Decompute

client = Decompute()

client.devices.scan()

huge_log = "\n".join([f"ERROR line {i}: file not found at /tmp/missing/{i}" for i in range(2000)])

preview = client.compression.preview([
    {"role": "tool", "content": huge_log},
    {"role": "user", "content": "Summarize the failure."},
])
print("Compression:", preview["tokens_before"], "->", preview["tokens_after"])

plan = client.routes.plan(
    model="claude-sonnet-4-5",
    messages=[{"role": "tool", "content": huge_log}, {"role": "user", "content": "Summarize the failure."}],
)
print("Yellowstone route:", plan["route"])

resp = client.chat.completions.create(
    model="claude-sonnet-4-5",
    messages=[{"role": "tool", "content": huge_log}, {"role": "user", "content": "Summarize the failure."}],
)
print(resp["choices"][0]["message"]["content"])
print(resp["usage"])
