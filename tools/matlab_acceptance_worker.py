"""Run an installed-wheel check in an owned or explicitly selected MATLAB session."""

import argparse
import io
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path


def _matlab_process_ids() -> set[int]:
    """Observe processes separately from Engine sharing; fail closed."""
    result = subprocess.run([
        "powershell", "-NoProfile", "-NonInteractive", "-Command",
        "@(Get-Process MATLAB -ErrorAction SilentlyContinue | "
        "Select-Object -ExpandProperty Id) | ConvertTo-Json -Compress",
    ], capture_output=True, text=True, check=True, timeout=30)
    value = json.loads(result.stdout) if result.stdout.strip() else []
    return {int(pid) for pid in (value if isinstance(value, list) else [value])}


def _com_worker(expected_root: str, expression: str, pid: int) -> int:
    """Attach to an explicitly selected, already running desktop; never create one."""
    if pid <= 0 or pid not in _matlab_process_ids():
        raise RuntimeError(f"Selected MATLAB PID is not running: {pid}")
    import win32com.client

    application = win32com.client.GetActiveObject("Matlab.Desktop.Application")
    quote = lambda value: str(value).replace("'", "''")
    # Run the acceptance in a function workspace, not the user's base workspace.
    with tempfile.TemporaryDirectory(prefix="radia-com-worker-", dir=r"C:\temp") as temporary:
        script = Path(temporary) / "verify_existing_session.m"
        receipt = Path(temporary) / "completed.json"
        script.write_text(f"""verify_existing_session_local();
function verify_existing_session_local()
assert(feature('getpid')=={pid}, 'Unexpected MATLAB PID');
assert(strcmpi(matlabroot,'{quote(expected_root)}'), 'Unexpected MATLAB root');
assert(isempty(find_system('SearchDepth',0,'Type','block_diagram')), 'Borrowed MATLAB has loaded diagrams');
[~, loadedMex] = inmem;
assert(~any(contains(string(loadedMex),["radia_mex","optuna_mex"])), 'Borrowed MATLAB has native handles');
savedPath=path; savedPwd=pwd;
names={{'PATH','MKL_THREADING_LAYER','PYTHONPATH','RADIA_PYTHON_EXECUTABLE'}};
values=cellfun(@getenv,names,'UniformOutput',false);
cleanup=onCleanup(@() restore_session(savedPath,savedPwd,names,values));
setenv('RADIA_PYTHON_EXECUTABLE','{quote(sys.executable)}');
{expression}
clear cleanup;
assert(strcmp(path,savedPath) && strcmp(pwd,savedPwd), 'Session paths were not restored');
assert(all(cellfun(@(n,v) strcmp(getenv(n),v),names,values)), 'Session environment was not restored');
fid=fopen('{quote(receipt)}','w'); assert(fid~=-1);
closeFile=onCleanup(@() fclose(fid));
fprintf(fid,'%s',jsonencode(struct('pid',feature('getpid'),'restored',true)));
end
function restore_session(savedPath,savedPwd,names,values)
clear radia_mex optuna_mex;
path(savedPath); cd(savedPwd);
for k=1:numel(names), setenv(names{{k}},values{{k}}); end
end
""", encoding="utf-8")
        output = application.Execute(f"run('{quote(script)}');")
        print(output, end="", flush=True)
        # Execute returns MATLAB errors as text. Require a fresh receipt written
        # only after the complete expression and session restoration succeed.
        if not receipt.is_file():
            raise RuntimeError("COM acceptance or borrowed-session restoration failed")
        proof = json.loads(receipt.read_text(encoding="utf-8"))
        if proof != {"pid": pid, "restored": True}:
            raise RuntimeError("Invalid COM completion receipt")
    print(f"MATLAB COM connected: PID={pid}; borrowed session restored", flush=True)
    return 0


def _engine_worker(expected_root: str, expression: str, session: str | None = None) -> int:
    """Reuse an explicitly named session, or own one when no MATLAB exists."""
    if session and session.startswith("com:"):
        if not re.fullmatch(r"com:[1-9][0-9]*", session):
            raise ValueError("COM session must be com:<positive PID>")
        return _com_worker(expected_root, expression, int(session[4:]))
    import matlab.engine

    processes = _matlab_process_ids()
    shared = tuple(matlab.engine.find_matlab())
    if session:
        if session not in shared:
            raise RuntimeError(f"Requested MATLAB Engine is not shared: {session}; available={shared}")
        engine = matlab.engine.connect_matlab(session)
    else:
        if processes or shared:
            raise RuntimeError(
                f"MATLAB already exists (PIDs={sorted(processes)}, shared={shared}); "
                "select an existing session with --engine-session. No new MATLAB was started.")
        # Sharing discovery is not evidence that an unshared process is absent.
        if _matlab_process_ids():
            raise RuntimeError("MATLAB started during preflight; retry session discovery")
        print("Starting dedicated MATLAB Engine session", flush=True)
        engine = matlab.engine.start_matlab("-nodesktop -nosplash")
    output, errors = io.StringIO(), io.StringIO()
    saved_path = saved_pwd = None
    borrowed_ready = False
    saved_environment = {}
    try:
        if session:
            pid = int(engine.feature("getpid"))
            if pid not in processes:
                raise RuntimeError(f"Shared MATLAB PID {pid} was not observed in process preflight")
            loaded = engine.find_system("SearchDepth", 0.0, "Type", "block_diagram")
            if loaded:
                raise RuntimeError("Borrowed MATLAB has loaded Simulink diagrams; refusing to disturb them")
            _, mex_files = engine.inmem(nargout=2)
            if any(Path(str(name)).stem in {"radia_mex", "optuna_mex"} for name in mex_files):
                raise RuntimeError("Borrowed MATLAB already has Radia/Optuna MEX loaded; preserving its handles")
            saved_path, saved_pwd = engine.path(), engine.pwd()
            saved_environment = {name: engine.getenv(name) for name in (
                "PATH", "MKL_THREADING_LAYER", "PYTHONPATH", "RADIA_PYTHON_EXECUTABLE")}
            borrowed_ready = True
            print(f"Reusing MATLAB Engine {session}, PID={pid}", flush=True)
        # radia.setup reads this; the candidate is this interpreter, never PATH's python.
        engine.setenv("RADIA_PYTHON_EXECUTABLE", sys.executable, nargout=0)
        print(f"RADIA_PYTHON_EXECUTABLE={sys.executable}", flush=True)
        actual_root = engine.matlabroot()
        if Path(actual_root).resolve() != Path(expected_root).resolve():
            raise RuntimeError(
                f"MATLAB Engine root mismatch: expected {expected_root}, got {actual_root}"
            )
        print(f"MATLAB Engine connected: {actual_root}", flush=True)
        engine.eval(expression, nargout=0, stdout=output, stderr=errors)
    finally:
        print(output.getvalue(), end="", flush=True)
        print(errors.getvalue(), end="", file=sys.stderr, flush=True)
        if session:
            try:
                if borrowed_ready:
                    engine.eval("clear radia_mex optuna_mex", nargout=0)
            finally:
                if saved_path is not None:
                    engine.path(saved_path, nargout=0)
                    engine.cd(saved_pwd, nargout=0)
                    for name, value in saved_environment.items():
                        engine.setenv(name, value, nargout=0)
        else:
            engine.quit()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("matlab_root")
    parser.add_argument("expression")
    parser.add_argument("session", nargs="?")
    args = parser.parse_args()
    raise SystemExit(_engine_worker(args.matlab_root, args.expression, args.session))
