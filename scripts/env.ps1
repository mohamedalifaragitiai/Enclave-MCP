# Session environment for M:\MCP_Project
# Enforces resource-governance.md section 1: C:\ receives zero writes.
# Dot-source this at the start of every session:  . M:\MCP_Project\scripts\env.ps1

$ProjectRoot = "M:\MCP_Project"

# Package / model caches - all redirected onto M:\
$env:UV_CACHE_DIR          = "$ProjectRoot\.cache\uv"
$env:UV_PYTHON_INSTALL_DIR = "$ProjectRoot\.cache\uv-python"
# Without this, `uv python install` drops a shim in C:\Users\<you>\.local\bin
$env:UV_PYTHON_BIN_DIR     = "$ProjectRoot\.tools\bin"
$env:HF_HOME               = "$ProjectRoot\.cache\huggingface"
# fastembed downloads ONNX weights to its own cache, not HF_HOME
$env:FASTEMBED_CACHE_PATH  = "$ProjectRoot\.cache\fastembed"
# ChromaDB phones home by default - HLD.md section 7 requires zero network egress
$env:ANONYMIZED_TELEMETRY  = "False"
# LangSmith ships with LangChain and traces to the cloud unless disabled
$env:LANGSMITH_TRACING     = "false"
$env:LANGCHAIN_TRACING_V2  = "false"

# The one permitted inference endpoint (resource-governance.md section 3)
$env:LLM_ENDPOINT          = "http://localhost:8001/v1"
$env:LLM_MODEL             = "Qwen3-4B-Instruct-2507"
$env:PIP_CACHE_DIR         = "$ProjectRoot\.cache\pip"

# Temp redirected too - pip/uv unpack wheels here and would otherwise hit C:\
$env:TMP  = "$ProjectRoot\.cache\tmp"
$env:TEMP = "$ProjectRoot\.cache\tmp"

$env:DOCKER_BUILDKIT = "1"

# Project-local uv, so nothing resolves to a C:\ toolchain
if ($env:PATH -notlike "*$ProjectRoot\.tools\uv*") {
    $env:PATH = "$ProjectRoot\.tools\uv;$env:PATH"
}

Write-Host "env loaded:" -ForegroundColor Green
"  UV_CACHE_DIR          = $env:UV_CACHE_DIR"
"  UV_PYTHON_INSTALL_DIR = $env:UV_PYTHON_INSTALL_DIR"
"  UV_PYTHON_BIN_DIR     = $env:UV_PYTHON_BIN_DIR"
"  HF_HOME               = $env:HF_HOME"
"  PIP_CACHE_DIR         = $env:PIP_CACHE_DIR"
"  TMP/TEMP              = $env:TMP"
