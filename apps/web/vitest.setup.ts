import "@testing-library/jest-dom"

// jsdom does not define PointerEvent, but @base-ui/react's Checkbox emits
// pointer events internally. Polyfill with a MouseEvent shape that exposes
// the pointer-event fields the libraries read.
if (typeof globalThis.PointerEvent === "undefined") {
  class PointerEventPolyfill extends MouseEvent {
    pointerId: number
    width: number
    height: number
    pressure: number
    tangentialPressure: number
    tiltX: number
    tiltY: number
    twist: number
    pointerType: string
    isPrimary: boolean

    constructor(type: string, init: PointerEventInit = {}) {
      super(type, init)
      this.pointerId = init.pointerId ?? 0
      this.width = init.width ?? 1
      this.height = init.height ?? 1
      this.pressure = init.pressure ?? 0
      this.tangentialPressure = init.tangentialPressure ?? 0
      this.tiltX = init.tiltX ?? 0
      this.tiltY = init.tiltY ?? 0
      this.twist = init.twist ?? 0
      this.pointerType = init.pointerType ?? ""
      this.isPrimary = init.isPrimary ?? false
    }
  }
  // @ts-expect-error — assigning the polyfill to the global PointerEvent slot
  globalThis.PointerEvent = PointerEventPolyfill
}

// Element.hasPointerCapture / setPointerCapture / releasePointerCapture are
// not implemented by jsdom; Base UI calls them defensively. Stub them out.
if (typeof Element !== "undefined") {
  if (!Element.prototype.hasPointerCapture) {
    Element.prototype.hasPointerCapture = function (): boolean {
      return false
    }
  }
  if (!Element.prototype.setPointerCapture) {
    Element.prototype.setPointerCapture = function (): void {
      /* no-op */
    }
  }
  if (!Element.prototype.releasePointerCapture) {
    Element.prototype.releasePointerCapture = function (): void {
      /* no-op */
    }
  }
}
