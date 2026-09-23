let
  system = "x86_64-linux";
  hyprlandFlake = builtins.getFlake "github:hyprwm/Hyprland/a0136d8c04687bb36eb8a28eb9d1ff92aea99704";
  pkgs = import hyprlandFlake.inputs.nixpkgs {
    inherit system;
    overlays = [ hyprlandFlake.overlays.hyprland-packages ];
  };
  hyprland = hyprlandFlake.packages.${system}.hyprland;
  python = pkgs.python3.withPackages (pythonPackages: [ pythonPackages.pygobject3 ]);
  gtkTypelibPath = pkgs.lib.makeSearchPath "lib/girepository-1.0" [
    pkgs.gdk-pixbuf
    pkgs.glib.out
    pkgs.graphene
    pkgs.gobject-introspection
    pkgs.gtk4
    pkgs.harfbuzz
    pkgs.pango.out
  ];
  pluginPkgConfigPackages = [
    hyprland
    hyprland.dev
    pkgs.aquamarine.dev
    pkgs.cairo.dev
    pkgs.hyprcursor.dev
    pkgs.hyprgraphics.dev
    pkgs.hyprlang.dev
    pkgs.hyprutils.dev
    pkgs.libdrm.dev
    pkgs.libglvnd.dev
    pkgs.libinput.dev
    pkgs.libxcb.dev
    pkgs.libxcb-errors.dev
    pkgs.libxcb-wm.dev
    pkgs.libxkbcommon.dev
    pkgs.pixman
    pkgs.systemd.dev
    pkgs.wayland.dev
  ];
  pluginPkgConfigPath = pkgs.lib.concatStringsSep ":" [
    (pkgs.lib.makeSearchPath "lib/pkgconfig" pluginPkgConfigPackages)
    (pkgs.lib.makeSearchPath "share/pkgconfig" pluginPkgConfigPackages)
  ];
in
pkgs.testers.runNixOSTest {
  name = "hyprland-background-computer-use-native-e2e";

  nodes.machine =
    { pkgs, ... }:
    {
      environment.systemPackages = [
        hyprland
        hyprland.dev
        pkgs.aquamarine.dev
        pkgs.cairo.dev
        pkgs.gcc15
        pkgs.glib.dev
        pkgs.grim
        pkgs.gtk4
        pkgs.hyprcursor.dev
        pkgs.hyprgraphics.dev
        pkgs.hyprlang.dev
        pkgs.hyprutils.dev
        pkgs.libdrm.dev
        pkgs.libglvnd.dev
        pkgs.libinput.dev
        pkgs.libxcb.dev
        pkgs.libxcb-errors.dev
        pkgs.libxcb-wm.dev
        pkgs.libxkbcommon.dev
        pkgs.pixman
        pkgs.pkg-config
        pkgs.systemd.dev
        pkgs.wayland.dev
        python
      ];

      environment.etc."codex-hyprland-e2e".source = ../.;
      programs.hyprland = {
        enable = true;
        package = hyprland;
      };
      services.getty.autologinUser = "alice";
      services.speechd.enable = false;
      users.users.alice.isNormalUser = true;
      xdg.portal.enable = pkgs.lib.mkForce false;

      virtualisation = {
        cores = 4;
        memorySize = 8192;
        resolution = {
          x = 1280;
          y = 720;
        };
        qemu.options = [ "-vga none -device virtio-gpu-pci" ];
      };
      system.stateVersion = "24.11";
    };

  testScript = ''
    machine.wait_for_unit("multi-user.target")
    machine.wait_for_unit("getty@tty1.service")
    status, output = machine.execute(
      "su - alice -c 'AQ_NO_KMS_REQUIREMENT=1 "
      "GI_TYPELIB_PATH=${gtkTypelibPath} "
      "PKG_CONFIG_PATH=${pluginPkgConfigPath} "
      "PYTHONPATH=/etc/codex-hyprland-e2e/src "
      "python /etc/codex-hyprland-e2e/tests/native_e2e.py --drm'"
    )
    print(output)
    assert status == 0, f"native Hyprland smoke test failed with status {status}"
    machine.shutdown()
  '';
}
