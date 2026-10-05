(function () {
  const form = document.getElementById("split-form");
  if (!form) return;
  const parent = Number(form.dataset.parentAmount || "0");
  const remaining = document.getElementById("split-remaining");
  function cents(value) {
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) return 0;
    return Math.round(parsed * 100);
  }
  function update() {
    let total = 0;
    form.querySelectorAll(".split-part-amount").forEach(function (input) {
      total += cents(input.value);
    });
    const left = parent - total;
    remaining.textContent = "Remaining: " + (left / 100).toFixed(2);
  }
  form.addEventListener("input", update);
  update();
})();
