param(
    [ValidateSet("smoke", "full")]
    [string]$Mode = "smoke",

    [ValidateSet("flagembedding", "cross-encoder", "qwen3", "none")]
    [string]$Reranker = "flagembedding",

    [string]$RerankerModel = "BAAI/bge-reranker-v2-m3",

    [int]$TopK = 100,
    [int]$FinalClaimTopK = 15,
    [int]$ParentTopK = 25,
    [int]$PerClaimPool = 3,
    [int]$SmokeClaims = 3,

    [switch]$RegenerateClaims,
    [switch]$SkipDense,
    [switch]$SkipGapRegistry
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$CondaEnvironment = "langchain0.3"
$DataDir = Join-Path $PSScriptRoot "data"
$ReportDir = Join-Path $PSScriptRoot "reports"
$ScriptDir = Join-Path $PSScriptRoot "scripts"

$ClaimsPath = Join-Path $DataDir "gold_atomic_claims_v9_v1.json"
$FullRetrievalPath = Join-Path $DataDir "atomic_retrieval_results_v9_v1.json"
$SmokeRetrievalPath = Join-Path $DataDir "atomic_retrieval_bge_smoke.json"
$RetrievalPath = if ($Mode -eq "full") { $FullRetrievalPath } else { $SmokeRetrievalPath }
$ParentOutputPath = if ($Mode -eq "full") {
    Join-Path $DataDir "parent_chunk_evidence_v9_v1.json"
} else {
    Join-Path $DataDir "parent_chunk_evidence_smoke_v9_v1.json"
}
$ParentHtmlPath = if ($Mode -eq "full") {
    Join-Path $ReportDir "parent_chunk_evidence_review_v9_v1.html"
} else {
    Join-Path $ReportDir "parent_chunk_evidence_smoke_review_v9_v1.html"
}

function Invoke-Step {
    param(
        [string]$Name,
        [string[]]$Arguments
    )

    Write-Host ""
    Write-Host "============================================================"
    Write-Host "STEP: $Name"
    Write-Host "============================================================"
    & conda run -n $CondaEnvironment python @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Step failed: $Name (exit code $LASTEXITCODE)"
    }
}

function Assert-File {
    param([string]$Path, [string]$Description)
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "$Description not found: $Path"
    }
}

Push-Location $ProjectRoot
try {
    Write-Host "Atomic RAG pipeline v1"
    Write-Host "Project: $ProjectRoot"
    Write-Host "Conda environment: $CondaEnvironment"
    Write-Host "Mode: $Mode"
    Write-Host "Dense enabled: $(-not $SkipDense)"
    Write-Host "Reranker: $Reranker"
    Write-Host "Reranker model: $RerankerModel"

    Invoke-Step "Verify environment and GPU" @(
        "-c",
        "import torch; print('torch=', torch.__version__); print('cuda=', torch.cuda.is_available()); print('device=', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CPU')"
    )

    if (-not $SkipDense -and -not $env:DASHSCOPE_API_KEY) {
        throw "DASHSCOPE_API_KEY is required for Dense retrieval. Set it first or use -SkipDense."
    }

    if ($RegenerateClaims) {
        Invoke-Step "Regenerate retrieval query units" @(
            (Join-Path $ScriptDir "prepare_atomic_gold_v1.py")
        )
    } else {
        Assert-File $ClaimsPath "Atomic claim dataset"
        Write-Host ""
        Write-Host "Using existing retrieval query units: $ClaimsPath"
        Write-Host "Use -RegenerateClaims only when automatic extraction should overwrite this file."
    }

    Invoke-Step "Build retrieval-query review HTML" @(
        (Join-Path $ScriptDir "build_atomic_claim_review_html_v1.py"),
        "--input", $ClaimsPath,
        "--output", (Join-Path $ReportDir "atomic_claim_review_v9_v1.html")
    )

    $RetrieveArguments = @(
        (Join-Path $ScriptDir "retrieve_atomic_claims_v1.py"),
        "--claims", $ClaimsPath,
        "--output", $RetrievalPath,
        "--top-k", "$TopK",
        "--final-top-k", "$FinalClaimTopK",
        "--reranker", $Reranker
    )
    if ($Reranker -ne "none") {
        $RetrieveArguments += @("--reranker-model", $RerankerModel)
    }
    if (-not $SkipDense) {
        $RetrieveArguments += "--dense"
    }
    if ($Mode -eq "smoke") {
        $RetrieveArguments += @("--max-claims", "$SmokeClaims")
    }

    Invoke-Step "Retrieve and rerank atomic query units" $RetrieveArguments

    Invoke-Step "Merge evidence back to parent chunks" @(
        (Join-Path $ScriptDir "merge_claim_evidence_to_parent_v1.py"),
        "--claims", $ClaimsPath,
        "--retrieval", $RetrievalPath,
        "--output", $ParentOutputPath,
        "--html", $ParentHtmlPath,
        "--per-claim-pool", "$PerClaimPool",
        "--final-top-k", "$ParentTopK"
    )

    if (-not $SkipGapRegistry) {
        Invoke-Step "Build knowledge-base gap registry" @(
            (Join-Path $ScriptDir "build_kb_gap_registry_v1.py"),
            "--input", $ClaimsPath,
            "--output", (Join-Path $DataDir "kb_gap_registry_v1.json")
        )
    }

    Write-Host ""
    Write-Host "Pipeline completed."
    Write-Host "Retrieval output: $RetrievalPath"
    Write-Host "Parent evidence output: $ParentOutputPath"
    Write-Host "Parent evidence review: $ParentHtmlPath"
    Write-Host "Atomic query review: $(Join-Path $ReportDir 'atomic_claim_review_v9_v1.html')"
}
finally {
    Pop-Location
}
