const startButton = document.getElementById("startButton");
const stopButton = document.getElementById("stopButton");
const wsUrlInput = document.getElementById("wsUrl");
const statusEl = document.getElementById("status");
const sessionStateEl = document.getElementById("sessionState");
const temperatureValueEl = document.getElementById("temperatureValue");
const imageEl = document.getElementById("streamImage");
const placeholderEl = document.getElementById("placeholder");

let socket = null;
let currentObjectUrl = null;
let isManualStop = false;
let sessionEndedByServer = false;
let closeStatusOverride = null;
let closeSessionStateOverride = null;

function defaultWebSocketUrl() {
  const pageProtocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const host = window.location.hostname || "localhost";
  return `${pageProtocol}//${host}:8765/stream`;
}

function normalizeWebSocketUrl(rawValue) {
  const trimmed = rawValue.trim();
  if (!trimmed) {
    return defaultWebSocketUrl();
  }

  if (trimmed.startsWith("ws://") || trimmed.startsWith("wss://")) {
    return trimmed;
  }

  if (trimmed.startsWith("http://") || trimmed.startsWith("https://")) {
    const parsed = new URL(trimmed);
    parsed.protocol = parsed.protocol === "https:" ? "wss:" : "ws:";
    if (!parsed.pathname || parsed.pathname === "/") {
      parsed.pathname = "/stream";
    }
    return parsed.toString();
  }

  const inferred = new URL(`${window.location.protocol}//${trimmed}`);
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  inferred.protocol = protocol;
  if (!inferred.pathname || inferred.pathname === "/") {
    inferred.pathname = "/stream";
  }
  return inferred.toString();
}

function setStatus(message, state) {
  statusEl.textContent = message;
  statusEl.dataset.state = state;
}

function setSessionState(message) {
  sessionStateEl.textContent = message;
}

function setTemperatureValue(value) {
  temperatureValueEl.textContent = value;
}

function cleanupImageUrl() {
  if (currentObjectUrl) {
    URL.revokeObjectURL(currentObjectUrl);
    currentObjectUrl = null;
  }
}

function clearStreamView() {
  cleanupImageUrl();
  imageEl.removeAttribute("src");
  placeholderEl.hidden = false;
}

function updateButtonState(isStreaming) {
  startButton.disabled = isStreaming;
  stopButton.disabled = !isStreaming;
}

function rememberCloseState(statusMessage, statusState, sessionMessage) {
  closeStatusOverride = { message: statusMessage, state: statusState };
  closeSessionStateOverride = sessionMessage;
}

function closeSocketIfNeeded() {
  if (socket) {
    socket.close();
    socket = null;
  }
}

wsUrlInput.value = defaultWebSocketUrl();
updateButtonState(false);
setSessionState("Idle");
setTemperatureValue("--.- C");

startButton.addEventListener("click", () => {
  let wsUrl;
  try {
    wsUrl = normalizeWebSocketUrl(wsUrlInput.value);
  } catch (_error) {
    setStatus("Enter a valid WebSocket server URL.", "error");
    return;
  }

  wsUrlInput.value = wsUrl;
  isManualStop = false;
  sessionEndedByServer = false;
  closeStatusOverride = null;
  closeSessionStateOverride = null;

  closeSocketIfNeeded();
  clearStreamView();

  setStatus("Connecting...", "pending");
  setSessionState("Connecting");
  setTemperatureValue("--.- C");
  updateButtonState(true);
  socket = new WebSocket(wsUrl);
  socket.binaryType = "blob";

  socket.addEventListener("open", () => {
    socket.send(JSON.stringify({ action: "start_session" }));
    setStatus("Starting coordinated session...", "pending");
    setSessionState("Starting");
  });

  socket.addEventListener("message", (event) => {
    if (typeof event.data === "string") {
      let payload;
      try {
        payload = JSON.parse(event.data);
      } catch (_error) {
        return;
      }

      switch (payload.type) {
        case "info":
          return;
        case "session_started":
          setStatus(`Session running for ${payload.durationSeconds} seconds.`, "ok");
          setSessionState("Running");
          return;
        case "temperature":
          if (typeof payload.celsius === "number") {
            setTemperatureValue(`${payload.celsius.toFixed(1)} C`);
          } else {
            setTemperatureValue("Sensor unavailable");
          }
          return;
        case "heater_state":
          if (payload.enabled) {
            setSessionState("Running / Heater ON");
          }
          return;
        case "session_busy":
          setStatus(payload.message || "Another session is already active.", "error");
          setSessionState("Busy");
          updateButtonState(false);
          rememberCloseState(payload.message || "Another session is already active.", "error", "Busy");
          closeSocketIfNeeded();
          return;
        case "session_error":
          setStatus(payload.message || "Session error.", "error");
          setSessionState("Error");
          rememberCloseState(payload.message || "Session error.", "error", "Error");
          return;
        case "session_ended":
          sessionEndedByServer = true;
          clearStreamView();
          setTemperatureValue("--.- C");
          setSessionState(`Ended: ${payload.reason}`);
          setStatus(`Session ended: ${payload.reason}.`, "pending");
          rememberCloseState(`Session ended: ${payload.reason}.`, "pending", `Ended: ${payload.reason}`);
          return;
        default:
          return;
      }
    }

    if (typeof event.data !== "string") {
      cleanupImageUrl();
      currentObjectUrl = URL.createObjectURL(event.data);
      imageEl.src = currentObjectUrl;
      placeholderEl.hidden = true;
      setStatus("Streaming live JPEG frames over WebSocket.", "ok");
      if (sessionStateEl.textContent === "Running") {
        setSessionState("Running / Video Active");
      }
      return;
    }
  });

  socket.addEventListener("close", () => {
    socket = null;
    clearStreamView();
    setTemperatureValue("--.- C");
    updateButtonState(false);
    if (isManualStop) {
      setSessionState("Stopped");
      setStatus("Stream stopped.", "pending");
      return;
    }
    if (closeStatusOverride) {
      setSessionState(closeSessionStateOverride || "Stopped");
      setStatus(closeStatusOverride.message, closeStatusOverride.state);
      closeStatusOverride = null;
      closeSessionStateOverride = null;
      return;
    }
    if (sessionEndedByServer) {
      return;
    }
    setSessionState("Disconnected");
    setStatus("Disconnected.", "error");
  });

  socket.addEventListener("error", () => {
    setSessionState("Error");
    updateButtonState(false);
    setStatus("Stream connection failed.", "error");
  });
});

stopButton.addEventListener("click", () => {
  if (!socket) {
    clearStreamView();
    updateButtonState(false);
    setStatus("Stream already stopped.", "pending");
    return;
  }

  isManualStop = true;
  sessionEndedByServer = false;
  setStatus("Stopping stream...", "pending");
  setSessionState("Stopping");
  try {
    socket.send(JSON.stringify({ action: "stop_session" }));
  } catch (_error) {
    // A disconnect-triggered cleanup is still enough to stop the session.
  }
  closeSocketIfNeeded();
});

window.addEventListener("beforeunload", () => {
  setTemperatureValue("--.- C");
  clearStreamView();
  closeSocketIfNeeded();
});
