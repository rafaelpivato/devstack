/* Form visibility/validation shared by compose and image task editors. */
for (const form of document.querySelectorAll("[data-container-task]")) {
  const target = form.querySelector(".target-picker");
  const service = form.querySelector(".service-options");
  const image = form.querySelector(".image-options");
  if (!target || !service || !image) continue;
  const imageInput = image.querySelector('input[name="image"]');
  const serviceInput = service.querySelector('select[name="service"]');
  const modeInput = service.querySelector('select[name="mode"]');
  function update() {
    const independent = target.value === "image";
    service.hidden = independent;
    image.hidden = !independent;
    if (serviceInput) serviceInput.disabled = independent;
    if (modeInput) modeInput.disabled = independent;
    if (imageInput) {
      imageInput.disabled = !independent;
      imageInput.required = independent;
    }
  }
  target.addEventListener("change", update);
  update();
}
