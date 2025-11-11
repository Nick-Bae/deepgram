// components/utils/throttle.ts

export type ThrottledFn<T extends (...args: any[]) => void> = ((
  ...args: Parameters<T>
) => void) & { cancel: () => void };

export function throttle<T extends (...args: any[]) => void>(
  func: T,
  delay: number
): ThrottledFn<T> {
  let lastCall = 0;

  const wrapped = ((...args: Parameters<T>) => {
    const now = Date.now();
    if (now - lastCall >= delay) {
      lastCall = now;
      func(...args);
    }
  }) as ThrottledFn<T>;

  wrapped.cancel = () => {
    lastCall = 0;
  };

  return wrapped;
}
  
