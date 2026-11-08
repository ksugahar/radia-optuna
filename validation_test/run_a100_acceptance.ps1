# The build job owns compilation. This job accepts only its exact wheel.
$ErrorActionPreference = 'Stop'
$root = (Get-Location).Path
$wheels = @(Get-ChildItem -LiteralPath 'dist/radia-optuna' -Filter '*.whl' -File)
if ($wheels.Count -ne 1 -or $env:EXPECTED_WHEEL_SHA256 -notmatch '^[0-9a-f]{64}$') {
    throw 'Expected one wheel and its build-job SHA-256.'
}
$wheel = $wheels[0].FullName
$sha256 = (Get-FileHash -LiteralPath $wheel -Algorithm SHA256).Hash.ToLowerInvariant()
if ($sha256 -cne $env:EXPECTED_WHEEL_SHA256) { throw 'Wheel differs from the build-job SHA-256.' }
py -3.12 verify_wheel.py $wheel --release-candidate --json
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$venv = Join-Path $env:RUNNER_TEMP "radia-optuna-gpu-$env:GITHUB_RUN_ID-$env:GITHUB_RUN_ATTEMPT"
py -3.12 -m venv $venv
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$py = Join-Path $venv 'Scripts/python.exe'
$env:PYTHONPATH = ''
$requirements = @(& $py -I -c "import tomllib; print(*tomllib.load(open('pyproject.toml','rb'))['project']['optional-dependencies']['native'], sep=chr(10))")
if ($LASTEXITCODE -ne 0 -or $requirements.Count -ne 1) { throw 'Expected the pinned native runtime requirement.' }
& $py -m pip install --disable-pip-version-check --no-deps $wheel @requirements
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$identity = & $py -I -c "import json, pathlib, radia_optuna; assert radia_optuna.layout() == 'wheel'; print(json.dumps({'matlab':str(radia_optuna.matlab_path()), 'receipt':str(pathlib.Path(radia_optuna.__file__).with_name('native_build.json'))}))"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$installed = $identity | ConvertFrom-Json
$build = Get-Content -LiteralPath $installed.receipt -Raw | ConvertFrom-Json
if ($build.source_sha -cne $env:GITHUB_SHA) { throw 'Wheel was built from another source SHA.' }

$out = Join-Path $root 'build-optuna-gpu-evidence'
New-Item -ItemType Directory -Path $out | Out-Null
& $py validation_test/validate_kernels.py --backend gpu --installed-wheel --build-receipt $installed.receipt --report "$out/python-gpu-verification.json"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$rootMatlab = $root.Replace("'", "''")
$wheelMatlab = $installed.matlab.Replace("'", "''")
$outMatlab = $out.Replace("'", "''")
$env:RADIA_OPTUNA_SOURCE_SHA = $env:GITHUB_SHA
$batch = @"
addpath('$wheelMatlab','-begin');
addpath('$rootMatlab/validation_test');
validate_binding_parity('$outMatlab/python-gpu-verification.json','$outMatlab/parity-gpu.json');
addpath('$rootMatlab/validation_test/radia_optuna');
validate_cuda('$rootMatlab','$wheelMatlab','$outMatlab/validation.json');
addpath('$rootMatlab/validation_test/optimization');
gpu=validate_optuna_cuda('$rootMatlab','$outMatlab/selection-validation.json','gpu','$wheelMatlab');
assert(gpu.ok && contains(gpu.gpu,'A100') && strcmp(gpu.compute_capability,'8.0'),'Expected an NVIDIA A100');
addpath('$rootMatlab/tests/matlab');
radia.optuna.executionMode('gpu');
result=test_standalone_simulink('$wheelMatlab');
assert(result.ok && strcmp(result.native.execution_mode,'gpu'));
fid=fopen('$outMatlab/installed-wheel-simulink.json','w');
assert(fid>=0);
fprintf(fid,'%s\n',jsonencode(result,PrettyPrint=true));
fclose(fid);
"@
& matlab -batch ($batch -replace '\r?\n',' ')
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$receipt = [ordered]@{source_sha=$env:GITHUB_SHA; wheel=$wheels[0].Name; wheel_sha256=$sha256; ok=$true}
[IO.File]::WriteAllText((Join-Path $out 'installed-wheel-identity.json'),($receipt | ConvertTo-Json)+"`n")
