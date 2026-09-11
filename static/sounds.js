/*
 * Small sound-effect layer for save conversions (sav <-> ktml).
 *
 * Three cues, matching static/sounds/:
 *   success -> conversion_success.wav  a sav<->ktml conversion completed
 *   error   -> conversion_error.wav    a sav<->ktml conversion failed
 *   warning -> warning_popup.wav       the user tried to upload/download a
 *                                      save that isn't there (no file
 *                                      selected, or nothing on disk yet)
 *
 * Whether sounds play at all is controlled by a "totk_sounds_enabled"
 * cookie (persists across visits, default on) and the toggle button in
 * the site header wires up to it.
 *
 * Two ways a page can ask this file to play a cue on load:
 *   1. <body data-play-sound="error">          -- for pages rendered
 *      directly by the server (e.g. error.html after a failed POST).
 *   2. a "?sound=success" query string          -- for redirects, where
 *      the server can't render a template but *can* build the URL. Read
 *      once, then stripped from the address bar so a refresh doesn't
 *      replay it.
 */
(function () {
  var SOUND_FILES = {
    success: "/static/sounds/conversion_success.wav",
    error: "/static/sounds/conversion_error.wav",
    warning: "/static/sounds/warning_popup.wav",
  };

  var COOKIE_NAME = "totk_sounds_enabled";

  function getCookie(name) {
    var match = document.cookie.match("(?:^|; )" + name + "=([^;]*)");
    return match ? decodeURIComponent(match[1]) : null;
  }

  function setCookie(name, value, days) {
    var maxAge = days * 24 * 60 * 60;
    document.cookie =
      name + "=" + encodeURIComponent(value) + "; max-age=" + maxAge + "; path=/; samesite=Lax";
  }

  function soundsEnabled() {
    var v = getCookie(COOKIE_NAME);
    return v === null ? true : v === "1";
  }

  function playSound(key) {
    if (!soundsEnabled()) return;
    var src = SOUND_FILES[key];
    if (!src) return;
    try {
      var audio = new Audio(src);
      // Autoplay can be blocked by the browser before the user has
      // interacted with the page at all -- harmless if it is, so just
      // swallow the rejection instead of surfacing a console error.
      audio.play().catch(function () {});
    } catch (e) {
      /* ignore -- sound is a nicety, never load-bearing */
    }
  }

  function initToggle() {
    var btn = document.getElementById("sound-toggle");
    if (!btn) return;

    function render() {
      var on = soundsEnabled();
      btn.textContent = on ? "\uD83D\uDD0A Sounds: On" : "\uD83D\uDD07 Sounds: Off";
      btn.setAttribute("aria-pressed", on ? "true" : "false");
    }

    render();
    btn.addEventListener("click", function () {
      setCookie(COOKIE_NAME, soundsEnabled() ? "0" : "1", 365);
      render();
    });
  }

  function initPageLoadSound() {
    var bodySound = document.body.getAttribute("data-play-sound");
    if (bodySound) {
      playSound(bodySound);
    }

    var params = new URLSearchParams(window.location.search);
    var querySound = params.get("sound");
    if (querySound) {
      playSound(querySound);
      params.delete("sound");
      var qs = params.toString();
      var newUrl = window.location.pathname + (qs ? "?" + qs : "") + window.location.hash;
      window.history.replaceState({}, "", newUrl);
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    initToggle();
    initPageLoadSound();
  });

  window.TotkSounds = { play: playSound, enabled: soundsEnabled };
})();
