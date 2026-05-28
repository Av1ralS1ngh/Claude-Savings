from decompute_ai import Decompute

client = Decompute()
print("health", client.get("/health"))
print("devices", client.devices.scan())

log = "\n".join(["FATAL service failed: permission denied /var/log/app.log"] * 1000)
preview = client.compression.preview([{"role": "tool", "content": log}, {"role": "user", "content": "what failed?"}])
print("compression", preview["tokens_before"], preview["tokens_after"], preview["compression_ratio"])

plan = client.routes.plan("claude-sonnet-4-5", [{"role": "tool", "content": log}, {"role": "user", "content": "what failed?"}])
print("plan", plan["route"], plan["compression_ratio"], plan["estimated_kv_cache_mb"])

dep = client.deployments.create("smoke-demo", plan)
print("deployment", dep["name"])

resp = client.chat.completions.create(
    model="claude-sonnet-4-5",
    messages=[{"role": "tool", "content": log}, {"role": "user", "content": "what failed?"}],
)
print("chat", resp["choices"][0]["message"]["content"])
print("usage", client.get("/v1/usage"))
