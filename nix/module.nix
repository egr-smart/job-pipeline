# NixOS module: scheduled `jobs collect` and `jobs digest` as systemd timers.
# Works without flakes: `imports = [ /path/to/job-pipeline/nix/module.nix ];`
{ config, lib, pkgs, ... }:

let
  cfg = config.services.job-pipeline;
  stateDir = "/var/lib/job-pipeline";
  configPath = "${cfg.configDir}/config.yaml";

  environment = {
    JOBS_CONFIG = configPath;
    JOBS_STATE_DIR = stateDir;
  };

  service = command: {
    description = "job-pipeline: ${command}";
    wants = [ "network-online.target" ];
    after = [ "network-online.target" ];
    inherit environment;
    serviceConfig = {
      Type = "oneshot";
      ExecStart = "${lib.getExe cfg.package} ${command}";
      User = "job-pipeline";
      Group = "job-pipeline";
      StateDirectory = "job-pipeline";
      StateDirectoryMode = "0700";
      WorkingDirectory = stateDir;
      EnvironmentFile = lib.mkIf (cfg.environmentFile != null) cfg.environmentFile;
      # Hardening: it only needs the network and its own state directory.
      NoNewPrivileges = true;
      PrivateTmp = true;
      PrivateDevices = true;
      ProtectSystem = "strict";
      ProtectHome = true;
      ProtectKernelTunables = true;
      ProtectKernelModules = true;
      ProtectControlGroups = true;
      RestrictAddressFamilies = [ "AF_INET" "AF_INET6" "AF_UNIX" ];
      RestrictNamespaces = true;
      LockPersonality = true;
      MemoryDenyWriteExecute = true;
    };
  };

  timer = schedule: {
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnCalendar = schedule;
      Persistent = true; # catch up on runs missed while the machine was off
      RandomizedDelaySec = "5m";
    };
  };

  # `sudo job-pipeline <args>` runs the CLI as the service user, with the service's
  # environment and secrets, so manual runs never leave root-owned files in the state dir.
  wrapper = pkgs.writeShellScriptBin "job-pipeline" ''
    if [ "$(id -u)" -ne 0 ]; then
      echo "run with sudo: sudo job-pipeline $*" >&2
      exit 1
    fi
    exec ${pkgs.systemd}/bin/systemd-run --quiet --pty --wait --collect \
      --uid=job-pipeline --gid=job-pipeline \
      -p StateDirectory=job-pipeline -p StateDirectoryMode=0700 -p WorkingDirectory=${stateDir} \
      ${lib.optionalString (cfg.environmentFile != null) "-p EnvironmentFile=${cfg.environmentFile}"} \
      -E JOBS_CONFIG=${configPath} -E JOBS_STATE_DIR=${stateDir} \
      ${lib.getExe cfg.package} "$@"
  '';
in
{
  options.services.job-pipeline = {
    enable = lib.mkEnableOption "job-pipeline scheduled job search";

    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.callPackage ./package.nix { };
      defaultText = lib.literalExpression "pkgs.callPackage ./package.nix { }";
      description = "The job-pipeline package.";
    };

    configDir = lib.mkOption {
      type = lib.types.path;
      example = lib.literalExpression "./job-pipeline";
      description = ''
        Directory containing config.yaml, profile.md and watchlist.yaml. Give it as a path
        literal (`./job-pipeline`, not a string) so it is copied into the Nix store; the
        service can't read /home. The store is world-readable: keep secrets out of this
        directory and use environmentFile instead.
        Writable paths in config.yaml (state_dir, reports, evals) are ignored in favour of
        ${stateDir}.
      '';
    };

    environmentFile = lib.mkOption {
      # A string, not a path, so the file is never copied into the Nix store.
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "/etc/job-pipeline.env";
      description = ''
        Root-readable KEY=VALUE file with secrets, e.g. OPENROUTER_API_KEY, SMTP_USER,
        SMTP_PASSWORD. Give it as a string so it stays out of the Nix store.
      '';
    };

    collect.schedule = lib.mkOption {
      type = lib.types.str;
      default = "daily";
      example = "*-*-* 06:00";
      description = "systemd OnCalendar expression for `jobs collect` (fetch, filter, score).";
    };

    digest.schedule = lib.mkOption {
      type = lib.types.str;
      default = "Mon 08:00";
      description = "systemd OnCalendar expression for `jobs digest` (report + email).";
    };
  };

  config = lib.mkIf cfg.enable {
    users.users.job-pipeline = {
      isSystemUser = true;
      group = "job-pipeline";
      home = stateDir;
    };
    users.groups.job-pipeline = { };

    systemd.services.job-pipeline-collect = service "collect";
    systemd.services.job-pipeline-digest = service "digest" // {
      # If both timers fire together, score first so the digest includes the newest jobs.
      after = [ "network-online.target" "job-pipeline-collect.service" ];
    };
    systemd.timers.job-pipeline-collect = timer cfg.collect.schedule;
    systemd.timers.job-pipeline-digest = timer cfg.digest.schedule;

    environment.systemPackages = [ wrapper ];
  };
}
