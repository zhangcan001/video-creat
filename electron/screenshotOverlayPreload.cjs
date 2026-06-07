const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("screenshotOverlay", {
  confirm: (payload) => ipcRenderer.invoke("screenshot:overlayConfirm", payload),
  cancel: () => ipcRenderer.invoke("screenshot:overlayCancel"),
  onStart: (callback) => {
    if (typeof callback !== "function") return () => {};
    const listener = (_event, payload) => {
      callback(payload);
    };
    ipcRenderer.on("screenshot:overlayStart", listener);
    return () => {
      ipcRenderer.removeListener("screenshot:overlayStart", listener);
    };
  },
  onReset: (callback) => {
    if (typeof callback !== "function") return () => {};
    const listener = () => {
      callback();
    };
    ipcRenderer.on("screenshot:overlayReset", listener);
    return () => {
      ipcRenderer.removeListener("screenshot:overlayReset", listener);
    };
  },
});
