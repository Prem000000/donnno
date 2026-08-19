// options.js — Settings page logic

const $ = (id) => document.getElementById(id);

const PROVIDERS = {
  gemini: {
    hint: 'Get a free API key from <a href="https://aistudio.google.com/app/apikey" target="_blank" class="link">Google AI Studio →</a>',
    models: [
      { id: "gemini-2.0-flash", label: "Gemini 2.0 Flash (Recommended · Fast · Free)" },
      { id: "gemini-2.0-flash-lite", label: "Gemini 2.0 Flash Lite (Fastest)" },
      { id: "gemini-1.5-pro", label: "Gemini 1.5 Pro (Most Accurate)" },
    ]
  },
  groq: {
    hint: 'Get a free API key from <a href="https://console.groq.com/keys" target="_blank" class="link">Groq Console →</a>',
    models: [
      { id: "openai/gpt-oss-120b", label: "GPT OSS 120B (Recommended · Best Quality)" },
      { id: "openai/gpt-oss-20b", label: "GPT OSS 20B (Fast & Good)" },
      { id: "qwen/qwen3.6-27b", label: "Qwen 3.6 27B (Good Alternative)" },
      { id: "meta-llama/llama-4-scout-17b-16e-instruct", label: "Llama 4 Scout 17B" },
    ]
  },
  openrouter: {
    hint: 'Get a free API key from <a href="https://openrouter.ai/keys" target="_blank" class="link">OpenRouter (Requires account) →</a>',
    models: [
      { id: "google/gemini-2.0-flash-lite-preview-02-05:free", label: "Gemini 2.0 Flash Lite (Free)" },
      { id: "meta-llama/llama-3-8b-instruct:free", label: "Llama 3 8B Instruct (Free)" },
      { id: "mistralai/mistral-7b-instruct:free", label: "Mistral 7B (Free)" },
    ]
  }
};

// ─── Navigation ───────────────────────────────────────────────────────────
document.querySelectorAll(".nav-item").forEach((item) => {
  item.addEventListener("click", (e) => {
    e.preventDefault();
    const section = item.dataset.section;

    document.querySelectorAll(".nav-item").forEach((n) => n.classList.remove("active"));
    item.classList.add("active");

    document.querySelectorAll(".section").forEach((s) => (s.style.display = "none"));
    $(`section-${section}`).style.display = "block";
  });
});

// ─── On Load ──────────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
  loadSettings();

  $("provider-select").addEventListener("change", handleProviderChange);

  const keyInput = $("api-key-input");
  $("toggle-key").addEventListener("click", (e) => {
    e.preventDefault();
    const isPassword = keyInput.type === "password";
    keyInput.type = isPassword ? "text" : "password";
    $("eye-icon").innerHTML = isPassword
      ? `<path d="M17.94 17.94A10.07 10.07 0 0112 20c-7 0-11-8-11-8a18.45 18.45 0 015.06-5.94M9.9 4.24A9.12 9.12 0 0112 4c7 0 11 8 11 8a18.5 18.5 0 01-2.16 3.19m-6.72-1.07a3 3 0 11-4.24-4.24" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><path d="M1 1l22 22" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>`
      : `<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" stroke="currentColor" stroke-width="2"/><circle cx="12" cy="12" r="3" stroke="currentColor" stroke-width="2"/>`;
  });

  $("save-btn").addEventListener("click", saveApiSettings);
  $("test-btn").addEventListener("click", testConnection);
  $("save-behavior-btn").addEventListener("click", saveBehaviorSettings);
});

function handleProviderChange() {
  const provider = $("provider-select").value;
  const config = PROVIDERS[provider];
  
  $("api-key-hint").innerHTML = config.hint;
  const select = $("model-select");
  select.innerHTML = "";
  config.models.forEach(m => {
    const opt = document.createElement("option");
    opt.value = m.id;
    opt.textContent = m.label;
    select.appendChild(opt);
  });
}

// ─── Load Settings ────────────────────────────────────────────────────────
function loadSettings() {
  chrome.storage.sync.get(
    ["apiProvider", "apiKey", "groqApiKey", "geminiApiKey", "groqModel", "geminiModel", 
     "autoHighlight", "showExplanation", "showConfidence", "autoClick"],
    (result) => {
      const provider = result.apiProvider || "gemini";
      $("provider-select").value = provider;
      handleProviderChange();

      // Load the appropriate key
      if (provider === "groq" && result.groqApiKey) {
        $("api-key-input").value = result.groqApiKey;
        if (result.groqModel) {
          if (Array.from($("model-select").options).some(o => o.value === result.groqModel)) {
            $("model-select").value = result.groqModel;
          }
        }
      } else if (result.apiKey) {
        $("api-key-input").value = result.apiKey;
      } else if (result.geminiApiKey) {
        $("api-key-input").value = result.geminiApiKey;
      }

      if (result.geminiModel) {
        if (Array.from($("model-select").options).some(o => o.value === result.geminiModel)) {
          $("model-select").value = result.geminiModel;
        }
      }

      $("auto-highlight").checked  = result.autoHighlight  !== false;
      $("show-explanation").checked = result.showExplanation !== false;
      $("show-confidence").checked  = result.showConfidence  !== false;
      $("auto-click").checked       = result.autoClick === true;
    }
  );
}

// ─── Save API Settings ────────────────────────────────────────────────────
function saveApiSettings() {
  const provider = $("provider-select").value;
  const key      = $("api-key-input").value.trim();
  const model    = $("model-select").value;

  if (!key) {
    showToast("Please enter an API key.", "error");
    $("api-key-input").focus();
    return;
  }

  const saveData = {
    apiProvider: provider,
    apiKey: key,
  };

  // Save provider-specific keys
  if (provider === "groq") {
    saveData.groqApiKey = key;
    saveData.groqModel = model;
  } else if (provider === "gemini") {
    saveData.geminiApiKey = key;
    saveData.geminiModel = model;
  }

  chrome.storage.sync.set(saveData, () => {
    showToast("✓ Settings saved successfully!", "success");
  });
}

// ─── Test Connection ──────────────────────────────────────────────────────
async function testConnection() {
  const provider = $("provider-select").value;
  const key      = $("api-key-input").value.trim();
  const model    = $("model-select").value;
  const status   = $("key-status");

  if (!key) {
    showToast("Enter your API key first.", "error");
    return;
  }

  $("test-btn").textContent = "Testing...";
  $("test-btn").disabled = true;
  status.style.display = "none";

  try {
    let response;
    
    if (provider === "gemini") {
      response = await fetch(
        `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent?key=${key}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            contents: [{ parts: [{ text: "Reply with OK" }] }],
            generationConfig: { maxOutputTokens: 10 },
          }),
        }
      );
    } else if (provider === "groq") {
      response = await fetch("https://api.groq.com/openai/v1/chat/completions", {
        method: "POST",
        headers: { 
          "Content-Type": "application/json",
          "Authorization": `Bearer ${key}`
        },
        body: JSON.stringify({
          model: model || "openai/gpt-oss-120b",
          messages: [{ role: "user", content: "Reply with strictly two letters: OK" }],
          max_tokens: 10
        }),
      });
    } else {
      response = await fetch("https://openrouter.ai/api/v1/chat/completions", {
        method: "POST",
        headers: { 
          "Content-Type": "application/json",
          "Authorization": `Bearer ${key}`
        },
        body: JSON.stringify({
          model: model,
          messages: [{ role: "user", content: "Reply with strictly two letters: OK" }],
          max_tokens: 10
        }),
      });
    }

    if (response.ok) {
      status.className = "key-status success";
      status.textContent = "✓ Connection successful! Your API key is working.";
      status.style.display = "flex";
      showToast("✓ API key is valid and working!", "success");
    } else {
      const err = await response.json();
      status.className = "key-status error";
      status.textContent = "✗ " + (err?.error?.message || err?.message || `Error ${response.status}`);
      status.style.display = "flex";
      showToast("✗ API key test failed.", "error");
    }
  } catch (err) {
    status.className = "key-status error";
    status.textContent = "✗ Network error: " + err.message;
    status.style.display = "flex";
    showToast("✗ Network error.", "error");
  } finally {
    $("test-btn").innerHTML = `
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none">
        <path d="M5 3l14 9-14 9V3z" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
      Test Connection`;
    $("test-btn").disabled = false;
  }
}

// ─── Save Behavior Settings ───────────────────────────────────────────────
function saveBehaviorSettings() {
  chrome.storage.sync.set({
    autoHighlight:   $("auto-highlight").checked,
    showExplanation: $("show-explanation").checked,
    showConfidence:  $("show-confidence").checked,
    autoClick:       $("auto-click").checked,
  }, () => {
    showToast("✓ Behavior settings saved!", "success");
  });
}

// ─── Toast ────────────────────────────────────────────────────────────────
let toastTimer = null;
function showToast(message, type = "success") {
  const toast = $("toast");
  toast.textContent = message;
  toast.className = `toast ${type} show`;

  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    toast.classList.remove("show");
  }, 3000);
}
