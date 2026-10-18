import { expect, type Locator } from '@playwright/test';

/**
 * Waits until an image has loaded and decoded. A broken image still has the box its CSS gives
 * it, so visibility alone does not show that the file was served.
 */
export async function expectImageLoaded(image: Locator): Promise<void> {
  await expect
    .poll(() =>
      image.evaluate((element: HTMLImageElement) =>
        element.decode().then(
          () => true,
          () => false,
        ),
      ),
    )
    .toBe(true);
}

/** The rendered size of an element, rounded to whole pixels. */
export async function renderedSize(element: Locator): Promise<{ width: number; height: number }> {
  const box = await element.boundingBox();
  expect(box).not.toBeNull();
  return { width: Math.round(box?.width ?? 0), height: Math.round(box?.height ?? 0) };
}
