# Non-flake entry point: `nix-build` builds the package (and runs the tests).
{ pkgs ? import <nixpkgs> { } }:
pkgs.callPackage ./nix/package.nix { }
