"use strict";

function commandParts(command) {
  const text = String(command || "").trim();
  if (!text) return [];
  const parts = [];
  const pattern = /"([^"\\]*(?:\\.[^"\\]*)*)"|'([^']*)'|([^\s]+)/g;
  let match;
  while ((match = pattern.exec(text))) parts.push(match[1] || match[2] || match[3]);
  return parts;
}

function resolveCommand(command, pathExists = () => false) {
  const text = String(command || "").trim();
  if (!text) return null;
  if (pathExists(text)) return { bin: text, args: [] };
  const parts = commandParts(text);
  if (!parts.length) return null;
  return { bin: parts[0], args: parts.slice(1) };
}

module.exports = {
  commandParts,
  resolveCommand,
};
