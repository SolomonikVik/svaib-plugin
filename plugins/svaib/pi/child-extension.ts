/** Same policy in every required child; the parent's receipt owns child usage. */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import svaib from "./extension.ts";

export default function child(pi: ExtensionAPI) {
  svaib(pi, { child: true });
}
