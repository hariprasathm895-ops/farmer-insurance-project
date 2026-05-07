// Main UI helpers for AgriSmart AI.
// This file handles the mobile menu, multilingual toggle, and scroll reveal effects.

const menuToggle = document.getElementById("menuToggle");
const navLinks = document.getElementById("navLinks");
const langToggle = document.getElementById("langToggle");

if (menuToggle && navLinks) {
    menuToggle.addEventListener("click", () => {
        navLinks.classList.toggle("open");
    });
}

function applyLanguage(languageCode) {
    document.documentElement.lang = languageCode === "ta" ? "ta" : "en";

    document.querySelectorAll("[data-en][data-ta]").forEach((element) => {
        const text = languageCode === "ta" ? element.dataset.ta : element.dataset.en;
        if (text) {
            element.textContent = text;
        }
    });

    localStorage.setItem("agrismart-language", languageCode);
}

if (langToggle) {
    langToggle.addEventListener("click", () => {
        const current = localStorage.getItem("agrismart-language") || "en";
        applyLanguage(current === "en" ? "ta" : "en");
    });
}

applyLanguage(localStorage.getItem("agrismart-language") || "en");

const observer = new IntersectionObserver(
    (entries) => {
        entries.forEach((entry) => {
            if (entry.isIntersecting) {
                entry.target.classList.add("is-visible");
            }
        });
    },
    { threshold: 0.15 }
);

document.querySelectorAll(".reveal").forEach((element) => observer.observe(element));
