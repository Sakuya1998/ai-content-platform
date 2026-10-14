package api

import (
	"context"
	"github.com/sakuya/novel-agent/go-control-plane/internal/modules/identity"
	"github.com/sakuya/novel-agent/go-control-plane/internal/transport/http/response"
)

type ArtifactSigner interface{ Ready(context.Context) error }

var canonicalPublicPath = response.CanonicalPath
var PrincipalFromContext = identity.FromContext
