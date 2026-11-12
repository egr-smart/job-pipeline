{
  description = "Automated remote job search: fetch, filter, LLM-score, email a shortlist";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  # Thin wrapper: everything lives in nix/ so non-flake systems can use it directly.
  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      packages = forAllSystems (pkgs: {
        default = pkgs.callPackage ./nix/package.nix { };
      });

      nixosModules.default = import ./nix/module.nix;

      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell { packages = [ pkgs.uv pkgs.python3 ]; };
      });
    };
}
