# Open-Jev decision server (non-generative agent/tool router) — evaluation service.
# CUDA 12.8 wheels are the minimum that ship Blackwell (sm_120) kernels.
FROM pytorch/pytorch:2.8.0-cuda12.8-cudnn9-runtime

# gcc: Triton (used by flash-linear-attention) compiles its CUDA launcher at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends git gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*

ARG OPEN_JEV_COMMIT=ed45657bf726c3b77408942830e5578f99df904e
RUN git clone https://github.com/Zefan-Cai/Open-Jev.git /opt/open-jev \
    && git -C /opt/open-jev checkout ${OPEN_JEV_COMMIT}
WORKDIR /opt/open-jev
RUN pip install --no-cache-dir "transformers==5.10.2" "peft==0.19.1" "accelerate==1.13.0" \
        safetensors "huggingface_hub[cli]" \
    && pip install --no-cache-dir --no-deps -e .
# Qwen3.5's linear-attention layers fall back to slow torch code without this
# (pure Triton, no compiler needed).
RUN pip install --no-cache-dir flash-linear-attention

COPY entrypoint.sh /entrypoint.sh
RUN sed -i 's/\r$//' /entrypoint.sh && chmod +x /entrypoint.sh

ENV HF_HOME=/models/hf
EXPOSE 8791
ENTRYPOINT ["/entrypoint.sh"]
