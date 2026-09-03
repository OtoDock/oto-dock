#!/usr/bin/env python3
"""Benchmark Smart Turn v3.2 ONNX inference latency.

Runs inference on random audio of varying lengths and reports timing stats,
splitting feature extraction from ONNX inference to identify the bottleneck.

Also tests different thread counts to find the optimal setting.

Usage: python tools/benchmark_smart_turn.py
"""

import time
from importlib.resources import files

import numpy as np

# The Smart Turn ONNX now lives in the audio package (audio/models/), resolved the
# same way SmartTurnClassifier loads it — so this dev benchmark runs from any cwd.
MODEL_PATH = str(files("audio") / "models" / "smart-turn-v3.2-cpu.onnx")
NUM_ITERATIONS = 20
AUDIO_LENGTHS_S = [1, 2, 4, 8]
SAMPLE_RATE = 16000
THREAD_COUNTS = [1, 2, 4]


def bench_threads(num_threads: int):
    import onnxruntime as ort
    from transformers import WhisperFeatureExtractor

    print(f"\n{'='*70}")
    print(f"  Threads: intra_op={num_threads}, inter_op=1")
    print(f"{'='*70}")

    opts = ort.SessionOptions()
    opts.inter_op_num_threads = 1
    opts.intra_op_num_threads = num_threads
    opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

    session = ort.InferenceSession(MODEL_PATH, sess_options=opts, providers=["CPUExecutionProvider"])
    input_name = session.get_inputs()[0].name
    expected_shape = session.get_inputs()[0].shape
    mel_frames = expected_shape[2] if len(expected_shape) > 2 and isinstance(expected_shape[2], int) else 800

    fe = WhisperFeatureExtractor.from_pretrained("openai/whisper-small")

    # Warmup
    for _ in range(3):
        audio = np.random.randn(SAMPLE_RATE * 4).astype(np.float32) * 0.1
        features = fe(audio, sampling_rate=SAMPLE_RATE, return_tensors="np")
        mel = features.input_features[:, :, :mel_frames]
        session.run(None, {input_name: mel})

    print(f"\n{'Length':>8s}  {'FE (ms)':>10s}  {'ONNX (ms)':>10s}  {'Total (ms)':>10s}  {'Min (ms)':>10s}  {'Max (ms)':>10s}  {'Result':>10s}")
    print("-" * 76)

    for length_s in AUDIO_LENGTHS_S:
        fe_times = []
        onnx_times = []
        total_times = []
        last_prob = 0.0

        for _ in range(NUM_ITERATIONS):
            audio = np.random.randn(SAMPLE_RATE * length_s).astype(np.float32) * 0.1

            # Time feature extraction
            t0 = time.perf_counter()
            features = fe(audio, sampling_rate=SAMPLE_RATE, return_tensors="np")
            mel = features.input_features[:, :, :mel_frames]
            t1 = time.perf_counter()

            # Time ONNX inference
            logit = session.run(None, {input_name: mel})[0]
            t2 = time.perf_counter()

            fe_ms = (t1 - t0) * 1000
            onnx_ms = (t2 - t1) * 1000
            total_ms = (t2 - t0) * 1000

            fe_times.append(fe_ms)
            onnx_times.append(onnx_ms)
            total_times.append(total_ms)

            prob = float(1.0 / (1.0 + np.exp(-logit.item())))
            last_prob = prob

        fe_avg = sum(fe_times) / len(fe_times)
        onnx_avg = sum(onnx_times) / len(onnx_times)
        total_avg = sum(total_times) / len(total_times)
        total_min = min(total_times)
        total_max = max(total_times)
        label = f"p={last_prob:.3f}"
        print(f"{length_s:>6d}s  {fe_avg:>10.1f}  {onnx_avg:>10.1f}  {total_avg:>10.1f}  {total_min:>10.1f}  {total_max:>10.1f}  {label:>10s}")

    return total_avg  # Return last avg for summary


def main():
    import onnxruntime as ort

    print(f"Loading model: {MODEL_PATH}")
    # Quick probe for model info
    session = ort.InferenceSession(MODEL_PATH, providers=["CPUExecutionProvider"])
    print(f"ONNX providers: {session.get_providers()}")
    expected_shape = session.get_inputs()[0].shape
    print(f"Model expects mel shape: {expected_shape}")
    del session

    results = {}
    for threads in THREAD_COUNTS:
        avg = bench_threads(threads)
        results[threads] = avg

    print(f"\n{'='*70}")
    print("  Summary")
    print(f"{'='*70}")
    for threads, avg in results.items():
        print(f"  {threads} thread(s): ~{avg:.0f}ms avg total")
    print("\nTarget: < 50ms average with host CPU passthrough.")


if __name__ == "__main__":
    main()
