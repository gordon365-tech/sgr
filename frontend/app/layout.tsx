'use client';

import { ReactNode } from 'react';

export default function RootLayout({
  children,
}: {
  children: ReactNode;
}) {
  return (
    <html lang="en">
      <body className="bg-sgr-dark text-sgr-light">
        {children}
      </body>
    </html>
  );
}
