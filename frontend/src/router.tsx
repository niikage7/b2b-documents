import { createBrowserRouter, Navigate } from 'react-router';
import { RequireAuth, RootLayout } from './auth';
import { AppLayout } from './components/AppLayout';
import { BatchesPage } from './pages/BatchesPage';
import { BatchPage } from './pages/BatchPage';
import { DocumentPage } from './pages/document/DocumentPage';
import { LoginPage } from './pages/LoginPage';
import { NotFoundPage } from './pages/NotFoundPage';
import { ProcessingPage } from './pages/ProcessingPage';
import { RulesPage } from './pages/rules/RulesPage';
import { UploadPage } from './pages/UploadPage';

export const router = createBrowserRouter([
  {
    element: <RootLayout />,
    children: [
      { path: '/login', element: <LoginPage /> },
      {
        element: (
          <RequireAuth>
            <AppLayout />
          </RequireAuth>
        ),
        children: [
          { index: true, element: <Navigate to="/batches" replace /> },
          { path: '/batches', element: <BatchesPage /> },
          { path: '/upload', element: <UploadPage /> },
          { path: '/batches/:batchId/processing', element: <ProcessingPage /> },
          { path: '/batches/:batchId', element: <BatchPage /> },
          { path: '/documents/:documentId', element: <DocumentPage />, handle: { compactSidebar: true } },
          { path: '/rules', element: <RulesPage /> },
          { path: '*', element: <NotFoundPage /> },
        ],
      },
    ],
  },
]);
