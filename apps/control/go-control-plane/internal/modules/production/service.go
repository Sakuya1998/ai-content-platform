package production

import "context"

type Repository interface {
	CreateProduction(context.Context, string, Request) (Run, error)
	Production(context.Context, string, string) (Run, error)
	Productions(context.Context, string, string) ([]Run, error)
	AdvanceProduction(context.Context, string, string) (Run, error)
	CancelProduction(context.Context, string, string) (Run, error)
	ResumeProduction(context.Context, string, string, string, string) (Run, error)
}

type Service struct{ Repository Repository }

func (s Service) Create(ctx context.Context, workspace string, request Request) (Run, error) {
	Normalize(&request)
	if !Valid(request) {
		return Run{}, ErrMessageConflict
	}
	return s.Repository.CreateProduction(ctx, workspace, request)
}
func (s Service) Get(ctx context.Context, workspace, id string) (Run, error) {
	return s.Repository.Production(ctx, workspace, id)
}
func (s Service) List(ctx context.Context, workspace, project string) ([]Run, error) {
	return s.Repository.Productions(ctx, workspace, project)
}
func (s Service) Advance(ctx context.Context, workspace, id string) (Run, error) {
	return s.Repository.AdvanceProduction(ctx, workspace, id)
}
func (s Service) Cancel(ctx context.Context, workspace, id string) (Run, error) {
	return s.Repository.CancelProduction(ctx, workspace, id)
}
func (s Service) Resume(ctx context.Context, workspace, id, child, action string) (Run, error) {
	return s.Repository.ResumeProduction(ctx, workspace, id, child, action)
}
