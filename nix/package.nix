# The job-pipeline CLI (`jobs`), built from this repo with nixpkgs' Python packages.
# Works without flakes: `pkgs.callPackage ./nix/package.nix { }`.
{ lib, python3Packages }:

python3Packages.buildPythonApplication {
  pname = "job-pipeline";
  version = "0.1.0";
  pyproject = true;

  # Only what the build needs, so a local checkout's config.yaml, profile.md, jobs.db,
  # reports/ and evals/ never end up in the (world-readable) Nix store.
  src = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ../pyproject.toml
      ../README.md
      (lib.fileset.fileFilter (f: !(lib.hasSuffix ".pyc" f.name)) ../job_pipeline)
      (lib.fileset.fileFilter (f: !(lib.hasSuffix ".pyc" f.name)) ../tests)
    ];
  };

  build-system = [ python3Packages.hatchling ];

  dependencies = with python3Packages; [
    beautifulsoup4
    feedparser
    httpx
    markdown
    openai
    pydantic
    pyyaml
    typer
  ];

  # The test suite is offline (recorded fixtures), so it runs in the build sandbox.
  nativeCheckInputs = [ python3Packages.pytestCheckHook ];

  # nixpkgs may lag the minimum versions in pyproject.toml; the APIs used here are stable.
  pythonRelaxDeps = true;

  pythonImportsCheck = [ "job_pipeline.cli" ];

  meta = {
    description = "Automated remote job search: fetch, filter, LLM-score, email a shortlist";
    mainProgram = "jobs";
  };
}
