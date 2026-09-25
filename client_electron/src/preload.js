"use strict";

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("smsync", {
  copy: (text) => ipcRenderer.invoke("copy-text", text),
  onCopied: (cb) => ipcRenderer.on("copied", () => cb()),
  openMain: () => ipcRenderer.send("open-main"),
  dismiss: () => ipcRenderer.send("popup-dismiss"),
  onSms: (cb) => ipcRenderer.on("sms", (_e, payload) => cb(payload)),
  onDeleted: (cb) => ipcRenderer.on("sms-deleted", (_e, payload) => cb(payload)),
  onCall: (cb) => ipcRenderer.on("call", (_e, payload) => cb(payload)),
  onCallUpdate: (cb) => ipcRenderer.on("call-update", (_e, call) => cb(call)),
  onCallEvent: (cb) => ipcRenderer.on("call-event", (_e, call) => cb(call)),
  onSmsSent: (cb) => ipcRenderer.on("sms-sent", (_e, row) => cb(row)),
  onAgentStatus: (cb) => ipcRenderer.on("agent-status", (_e, s) => cb(s)),
  onStatus: (cb) => ipcRenderer.on("status", (_e, status) => cb(status)),
  getConfig: () => ipcRenderer.invoke("get-config"),
  relogin: () => ipcRenderer.invoke("relogin"),
  onUpdateAvailable: (cb) => ipcRenderer.on("update-available", (_e, v) => cb(v)),
  openReleases: () => ipcRenderer.send("open-releases"),
  getSettings: () => ipcRenderer.invoke("get-settings"),
  saveSettings: (patch) => ipcRenderer.invoke("save-settings", patch),
  testPopup: () => ipcRenderer.send("test-popup"),
  onShowSettings: (cb) => ipcRenderer.on("show-settings", () => cb()),
});
