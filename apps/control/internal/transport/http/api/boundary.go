package api

import (
	"context"
	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/identity"
	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/transport/http/response"
)

type ArtifactSigner interface{ Ready(context.Context) error }

var canonicalPublicPath = response.CanonicalPath
var PrincipalFromContext = identity.FromContext
