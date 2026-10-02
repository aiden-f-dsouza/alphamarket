// Koine: wallet signing for the ownership claim page (claim.html, wave2).
// CSP script-src 'self': no inline script, no on* attributes; everything is
// wired through data-* hooks. Without a browser wallet (or without JS) the
// page still works: the user signs elsewhere and pastes the signature.
//
// Flow: eth_requestAccounts, then personal_sign of the exact message the
// server rendered (UTF-8 bytes as 0x hex, EIP-191), then fill the form and
// submit it. The server rebuilds the message from the stored claim code and
// checks the signature; nothing here is trusted.
(function () {
  "use strict";
  var form = document.querySelector("[data-claim-form]");
  if (!form) return;
  var button = form.querySelector("[data-claim-sign]");
  var field = form.querySelector("[data-claim-signature]");
  var status = form.querySelector("[data-claim-status]");
  var source = form.querySelector("[data-claim-message]");
  var eth = window.ethereum;
  if (!button || !field || !source || !eth || typeof eth.request !== "function") return;

  function say(text) {
    if (status) status.textContent = text;
  }

  function utf8Hex(text) {
    var bytes = new TextEncoder().encode(text);
    var out = "0x";
    for (var i = 0; i < bytes.length; i++) {
      out += (bytes[i] < 16 ? "0" : "") + bytes[i].toString(16);
    }
    return out;
  }

  button.hidden = false;
  button.addEventListener("click", function () {
    var message = source.getAttribute("data-claim-message") || "";
    var wanted = (form.getAttribute("data-claim-address") || "").toLowerCase();
    button.disabled = true;
    say("Waiting for your wallet...");
    eth
      .request({ method: "eth_requestAccounts" })
      .then(function (accounts) {
        if (!accounts || !accounts.length) throw new Error("no account was shared");
        // Prefer the claimed address when the wallet holds it; a vault leader
        // or sub-account master signs with its own address instead.
        var signer = accounts[0];
        for (var i = 0; i < accounts.length; i++) {
          if (String(accounts[i]).toLowerCase() === wanted) signer = accounts[i];
        }
        return eth.request({ method: "personal_sign", params: [utf8Hex(message), signer] });
      })
      .then(function (signature) {
        field.value = signature;
        say("Signed. Checking...");
        if (typeof form.requestSubmit === "function") form.requestSubmit();
        else form.submit();
      })
      .catch(function (err) {
        button.disabled = false;
        say("The wallet did not sign" + (err && err.message ? ": " + err.message : "."));
      });
  });
})();
