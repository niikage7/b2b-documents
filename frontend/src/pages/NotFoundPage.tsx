import { FileQuestion } from 'lucide-react';
import { Link } from 'react-router';
import { EmptyState } from '../components/feedback';

export function NotFoundPage() {
  return (
    <div className="page">
      <EmptyState
        icon={<FileQuestion size={28} />}
        title="Страница не найдена"
        text="Возможно, ссылка устарела или в ней опечатка."
        actions={
          <Link to="/batches" className="btn btn--primary">
            К списку пакетов
          </Link>
        }
      />
    </div>
  );
}
