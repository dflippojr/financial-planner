(function () {
  function csrfToken() {
    var match = document.cookie.match(/(?:^|; )csrftoken=([^;]*)/);
    return match ? decodeURIComponent(match[1]) : "";
  }

  function b64ToBuf(value) {
    var padded = value.replace(/-/g, "+").replace(/_/g, "/");
    var pad = padded.length % 4;
    if (pad) {
      padded += "=".repeat(4 - pad);
    }
    var binary = atob(padded);
    var bytes = new Uint8Array(binary.length);
    for (var i = 0; i < binary.length; i += 1) {
      bytes[i] = binary.charCodeAt(i);
    }
    return bytes.buffer;
  }

  function bufToB64(buffer) {
    var bytes = new Uint8Array(buffer);
    var binary = "";
    for (var i = 0; i < bytes.byteLength; i += 1) {
      binary += String.fromCharCode(bytes[i]);
    }
    return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
  }

  function decodeOptions(options) {
    var copy = JSON.parse(JSON.stringify(options));
    copy.challenge = b64ToBuf(options.challenge);
    if (copy.user && copy.user.id) {
      copy.user.id = b64ToBuf(options.user.id);
    }
    (copy.excludeCredentials || []).forEach(function (item) {
      item.id = b64ToBuf(item.id);
    });
    (copy.allowCredentials || []).forEach(function (item) {
      item.id = b64ToBuf(item.id);
    });
    return copy;
  }

  function encodeCredential(credential) {
    var response = credential.response;
    var payload = {
      id: credential.id,
      rawId: bufToB64(credential.rawId),
      type: credential.type,
      clientExtensionResults: credential.getClientExtensionResults ? credential.getClientExtensionResults() : {},
      response: {
        clientDataJSON: bufToB64(response.clientDataJSON),
      },
    };
    if (response.attestationObject) {
      payload.response.attestationObject = bufToB64(response.attestationObject);
      payload.response.transports = response.getTransports ? response.getTransports() : [];
    }
    if (response.authenticatorData) {
      payload.response.authenticatorData = bufToB64(response.authenticatorData);
      payload.response.signature = bufToB64(response.signature);
      payload.response.userHandle = response.userHandle ? bufToB64(response.userHandle) : null;
    }
    return payload;
  }

  function showError(node, message) {
    if (!node) {
      return;
    }
    node.textContent = message;
    node.classList.remove("hidden");
  }

  function postJson(url, body) {
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": csrfToken(),
      },
      body: JSON.stringify(body || {}),
    }).then(function (response) {
      return response.json().then(function (data) {
        data.status = response.status;
        return data;
      });
    });
  }

  function register(root) {
    var errorNode = document.getElementById("passkey-register-error");
    var nameInput = document.getElementById("passkey-name");
    postJson(root.getAttribute("data-options-url"), {})
      .then(function (data) {
        if (data.status === 403 && data.reauth) {
          window.location = root.getAttribute("data-reauth-url");
          return null;
        }
        if (!data.ok) {
          showError(errorNode, data.error || "Passkey registration failed.");
          return null;
        }
        return navigator.credentials.create({ publicKey: decodeOptions(data.options) });
      })
      .then(function (credential) {
        if (!credential) {
          return null;
        }
        return postJson(root.getAttribute("data-register-url"), {
          name: nameInput ? nameInput.value : "",
          credential: encodeCredential(credential),
        });
      })
      .then(function (data) {
        if (!data) {
          return;
        }
        if (!data.ok) {
          showError(errorNode, data.error || "Passkey registration failed.");
          return;
        }
        window.location.reload();
      })
      .catch(function () {
        showError(errorNode, "Passkey registration failed.");
      });
  }

  function assertPasskey(button) {
    var errorNode = document.getElementById("passkey-assert-error");
    postJson(button.getAttribute("data-options-url"), {})
      .then(function (data) {
        if (!data.ok) {
          showError(errorNode, data.error || "Passkey verification failed.");
          return null;
        }
        return navigator.credentials.get({ publicKey: decodeOptions(data.options) });
      })
      .then(function (credential) {
        if (!credential) {
          return null;
        }
        return postJson(button.getAttribute("data-assert-url"), {
          credential: encodeCredential(credential),
          next: button.getAttribute("data-next") || "",
        });
      })
      .then(function (data) {
        if (!data) {
          return;
        }
        if (!data.ok) {
          showError(errorNode, data.error || "Passkey verification failed.");
          return;
        }
        window.location = data.redirect || "/";
      })
      .catch(function () {
        showError(errorNode, "Passkey verification failed.");
      });
  }

  document.addEventListener("click", function (event) {
    var registerRoot = event.target.closest("[data-passkey-register]");
    if (registerRoot) {
      var box = document.getElementById("passkey-register");
      if (box) {
        register(box);
      }
      return;
    }
    var assertButton = event.target.closest("[data-passkey-assert]");
    if (assertButton) {
      assertPasskey(assertButton);
    }
  });
})();
