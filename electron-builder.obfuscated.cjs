const { build } = require("./package.json");
const { execFile } = require("node:child_process");
const path = require("node:path");
const { Arch } = require("builder-util");
const { getRceditBundle } = require("app-builder-lib/out/toolsets/windows");

async function editWindowsExecutableResources(context) {
  if (context.electronPlatformName !== "win32") {
    return;
  }

  const appInfo = context.packager.appInfo;
  const exePath = path.join(context.appOutDir, `${appInfo.productFilename}.exe`);
  const iconPath = await context.packager.getIconPath();
  const bundle = await getRceditBundle("1.1.0");
  const rceditPath = context.arch === Arch.ia32 ? bundle.x86 : bundle.x64;
  const requestedExecutionLevel = context.packager.platformSpecificBuildOptions.requestedExecutionLevel;
  const args = [
    exePath,
    "--set-version-string",
    "FileDescription",
    appInfo.productName,
    "--set-version-string",
    "ProductName",
    appInfo.productName,
    "--set-version-string",
    "LegalCopyright",
    appInfo.copyright,
    "--set-file-version",
    appInfo.shortVersion || appInfo.buildVersion,
    "--set-product-version",
    appInfo.shortVersionWindows || appInfo.getVersionInWeirdWindowsForm(),
    "--set-version-string",
    "InternalName",
    path.basename(exePath, ".exe"),
    "--set-version-string",
    "OriginalFilename",
    "",
  ];

  if (appInfo.companyName) {
    args.push("--set-version-string", "CompanyName", appInfo.companyName);
  }

  if (context.packager.platformSpecificBuildOptions.legalTrademarks) {
    args.push(
      "--set-version-string",
      "LegalTrademarks",
      context.packager.platformSpecificBuildOptions.legalTrademarks
    );
  }

  if (requestedExecutionLevel && requestedExecutionLevel !== "asInvoker") {
    args.push("--set-requested-execution-level", requestedExecutionLevel);
  }

  if (iconPath) {
    args.push("--set-icon", iconPath);
  }

  await new Promise((resolve, reject) => {
    execFile(rceditPath, args, (error, stdout, stderr) => {
      if (error) {
        error.message = `${error.message}\n${stdout || ""}${stderr || ""}`;
        reject(error);
        return;
      }
      resolve();
    });
  });
}

module.exports = {
  ...build,
  electronDist: path.join(__dirname, "node_modules", "electron", "dist"),
  toolsets: {
    ...(build.toolsets || {}),
    winCodeSign: "1.1.0",
  },
  win: {
    ...(build.win || {}),
    signAndEditExecutable: false,
  },
  afterPack: editWindowsExecutableResources,
  directories: {
    ...(build.directories || {}),
    app: "release/obfuscated-code",
    output: "dist-obfuscated",
  },
};
