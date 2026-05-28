# SDK obfuscation

The SDK is intentionally thin. The proprietary logic should stay server-side:

- Headroom policy;
- Yellowstone routing;
- prompt-cache strategy;
- billing/usage logic;
- model/provider selection.

For on-prem/private distributions, you can build an obfuscated SDK wheel:

```bash
make sdk-obfuscate
```

This uses PyArmor. Python obfuscation raises reverse-engineering effort but is not a security boundary. Do not embed provider secrets or core algorithms in the SDK.
