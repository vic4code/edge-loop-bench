# MacBook Air hardware protocol

Apple Silicon uses one unified-memory pool for the CPU, GPU, model weights, KV cache, runtime, benchmark tools, and operating system. A reproducible run therefore needs more than the marketing chip name.

## Record before a run series

- exact chip and Mac model;
- performance/efficiency CPU cores and GPU core count;
- unified-memory capacity;
- macOS version and build;
- runtime architecture (`arm64`, never translated x86 for vLLM-Metal);
- power source and low-power-mode state;
- active displays and material background workloads;
- ambient temperature when practical;
- any manual wired-memory or kernel setting changes.

`edgeloop doctor --json` gathers safe non-privileged facts and runtime executable paths. Copy its output into a versioned run manifest and add facts it cannot discover reliably. The command never changes system settings.

## Run discipline

1. Use AC power and disable low-power mode.
2. Close competing compute-heavy processes.
3. Load the selected model and run documented warm-ups.
4. Randomize agent-run order in paired blocks.
5. Define a cooldown rule before data collection.
6. Observe memory pressure and swap, not just process RSS.
7. Record cold-load and warm-resident measurements separately.
8. Add a sustained 20–30 minute qualification because the MacBook Air is fanless.

## Memory qualification

Reject or label an operating point when it causes sustained swap, repeated model eviction, undisclosed CPU fallback, or severe run-order-dependent throttling. A quantized artifact fitting on disk or loading once is not sufficient.

## Energy

Energy collection is optional until one method can be applied consistently without elevated access or measurement distortion. If collected, report the tool, sampling rate, privileges, integration method, and baseline subtraction. Never compare energy numbers collected by different methods as if they were equivalent.
