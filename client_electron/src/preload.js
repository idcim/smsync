"use strict";

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("smsync", {
  copy: (text) => ipcRenderer.invoke("copy-text", text),
  onCopied: (cb) => ipcRenderer.on("copied", () => cb()),
  openMain: () => ipcRenderer.send("open-main"),
  dismiss: () => ipcRenderer.send("popup-dismiss"),
  onSms: (cb) => ipcRenderer.on("sms", (_e, payload) => cb(payload)),
  onStatus: (cb) => ipcRenderer.on("status", (_e, status) => cb(status)),
  getConfig: () => ipcRenderer.invoke("get-config"),
  getSettings: () => ipcRenderer.invoke("get-settings"),
  saveSettings: (patch) => ipcRenderer.invoke("save-settings", patch),
  testPopup: () => ipcRenderer.send("test-popup"),
  onShowSettings: (cb) => ipcRenderer.on("show-settings", () => cb()),
});
