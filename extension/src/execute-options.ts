/** Build the executor options for a content-script "execute" message.
 *
 * Extracted from content.ts so the fill-map plumbing is testable
 * (Sprint 5, frontend B1): the vault fill is a plain Record over
 * the message (a function cannot cross chrome.runtime.sendMessage),
 * and the executor's `fill` is a function. The raw Record must
 * NOT be spread into the options — options.fill is a function,
 * and a Record is truthy, so spreading it first makes the guard
 * never fire and executeAction calls a plain object as a function
 * (a TypeError at runtime, invisible to typecheck).
 */
import type { ExecuteOptions } from "./executor.js";
import type { ElementInfo } from "./vision/types.js";

export type ExecuteMessageOptions = ExecuteOptions & {
  expected?: ElementInfo;
  fill?: Record<string, string>;
};

export function buildExecuteOptions(
  msgOptions: ExecuteMessageOptions | undefined,
  fresh: ElementInfo | undefined,
): ExecuteOptions {
  const { fill: fillMap, ...restOptions } = msgOptions ?? {};
  const options: ExecuteOptions = {
    ...restOptions,
    expected: msgOptions?.expected ?? fresh,
  };
  if (fillMap) {
    options.fill = (token: string) => fillMap[token];
  }
  return options;
}
