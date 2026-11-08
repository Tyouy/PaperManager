"""Build a macOS application and a desktop shortcut for this checkout."""
from pathlib import Path
import plistlib
import json
import subprocess
import tempfile

BASE = Path(__file__).resolve().parent
IDENTIFIER = "local.paperwise.library"


def build_icon():
    import pymupdf
    icon = BASE / "assets" / "Paperwise.icns"
    with tempfile.TemporaryDirectory(prefix="paperwise-icon-") as temporary:
        temporary = Path(temporary)
        with pymupdf.open(BASE / "assets/icon.svg") as svg:
            pdf = svg.convert_to_pdf()
        with pymupdf.open("pdf", pdf) as document:
            document[0].get_pixmap(alpha=True).save(temporary / "source.png")
        iconset = temporary / "Paperwise.iconset"
        iconset.mkdir()
        for size in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                name = f"icon_{size}x{size}" + ("@2x" if scale == 2 else "") + ".png"
                subprocess.run(["sips", "-z", str(size * scale), str(size * scale),
                                str(temporary / "source.png"), "--out", str(iconset / name)],
                               check=True, capture_output=True)
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(icon)], check=True)
    return icon


def install():
    application = BASE / "文献检索.app"
    shortcut = Path.home() / "Desktop" / application.name
    if application.exists():
        info = plistlib.loads((application / "Contents/Info.plist").read_bytes())
        if info.get("CFBundleIdentifier") != IDENTIFIER:
            raise ValueError("已存在同名应用，未覆盖：" + str(application))
    if shortcut.exists() or shortcut.is_symlink():
        if not shortcut.is_symlink() or shortcut.resolve() != application:
            raise ValueError("桌面已存在同名项目，未覆盖：" + str(shortcut))
    application.mkdir(exist_ok=True)
    executable = application / "Contents/MacOS/PaperwiseLauncher"
    executable.parent.mkdir(parents=True, exist_ok=True)
    # Remove the obsolete executable when upgrading an earlier AppleScript build.
    (executable.parent / "applet").unlink(missing_ok=True)
    resources = application / "Contents/Resources"
    resources.mkdir(parents=True, exist_ok=True)
    # A native launcher avoids AppleScript automation and shell-app permissions.
    c_string = lambda value: json.dumps(str(value), ensure_ascii=False)
    script = '''#include <unistd.h>
#include <fcntl.h>
#include <stdlib.h>
int main(void) {
    pid_t child = fork();
    if (child < 0) return 1;
    if (child > 0) return 0;
    if (setsid() < 0) _exit(1);
    int input = open("/dev/null", O_RDONLY);
    int output = open(LOG_PATH, O_WRONLY | O_CREAT | O_APPEND, 0600);
    if (input >= 0) dup2(input, 0);
    if (output >= 0) { dup2(output, 1); dup2(output, 2); }
    if (chdir(PROJECT_PATH) < 0) _exit(1);
    execl(PYTHON_PATH, PYTHON_PATH, LAUNCHER_PATH, (char *)0);
    _exit(1);
}
'''
    defines = "\n".join("#define " + key + " " + c_string(value) for key, value in {
        "LOG_PATH": BASE / ".paperwise-desktop.log", "PROJECT_PATH": BASE,
        "PYTHON_PATH": BASE / ".venv/bin/python", "LAUNCHER_PATH": BASE / "desktop_launcher.py"}.items())
    with tempfile.TemporaryDirectory(prefix="paperwise-app-") as temporary:
        source = Path(temporary) / "launcher.c"
        source.write_text(defines + "\n" + script, encoding="utf-8")
        developer = subprocess.check_output(["xcode-select", "-p"], text=True).strip()
        sdk = Path(developer) / "SDKs/MacOSX.sdk"
        subprocess.run(["clang", "-isysroot", str(sdk), "-Os", "-o", str(executable), str(source)], check=True)
    info_file = application / "Contents/Info.plist"
    info = dict(CFBundleIdentifier=IDENTIFIER, CFBundleName="文献检索", CFBundleDisplayName="文献检索",
                CFBundleExecutable="PaperwiseLauncher", CFBundlePackageType="APPL",
                CFBundleIconFile="Paperwise", CFBundleIconName="Paperwise", LSUIElement=True,
                NSHighResolutionCapable=True, CFBundleShortVersionString="1.1")
    info_file.write_bytes(plistlib.dumps(info))
    icon = build_icon()
    (application / "Contents/Resources/Paperwise.icns").write_bytes(icon.read_bytes())
    subprocess.run(["codesign", "--force", "--sign", "-", str(application)], check=True, capture_output=True)
    subprocess.run(["codesign", "--verify", str(application)], check=True, capture_output=True)
    if not shortcut.is_symlink():
        shortcut.symlink_to(application, target_is_directory=True)
    subprocess.run(["touch", str(application)], check=True)
    print("已创建桌面启动图标：" + str(shortcut))


if __name__ == "__main__":
    install()
