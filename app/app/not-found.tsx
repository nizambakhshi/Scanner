import Link from "next/link";

/**
 * A missing symbol and a missing page get the same answer on purpose: nothing here
 * is public, so an error page should not confirm which instrument names exist in
 * the universe.
 */
export default function NotFound() {
  return (
    <main className="mx-auto flex min-h-[60vh] max-w-[560px] flex-col justify-center px-4">
      <p className="eyebrow">404</p>
      <h1 className="mt-1 text-[20px] font-semibold tracking-tight">Nothing at this address</h1>
      <p className="mt-2 text-[13px] leading-snug text-muted">
        The page does not exist, or the symbol you asked for is not in the instrument universe this worker has mapped.
        Neither case is answered differently, because which symbols exist is itself information.
      </p>
      <p className="mt-4 text-[13px]">
        <Link href="/" className="link">
          Back to the board
        </Link>
      </p>
    </main>
  );
}
