import json
import logging
from enum import Enum, auto
from typing import Any, ClassVar, Dict, Iterable, Iterator, List, Optional, Tuple, Union

from pydantic import BaseModel, validator, Field, BaseConfig
from pydantic.class_validators import Validator
from pydantic.fields import ModelField, PrivateAttr
from rich import print, inspect
from operator import attrgetter

from ..utils import qualname_base

log = logging.getLogger("rich")


class DynamicObject:
    def __init__(self, d: dict) -> None:
        self.__dict__.update(d)

    def __repr__(self) -> str:
        return str(self.__dict__)

    @classmethod
    def from_dict(cls, d: dict) -> "DynamicObject":
        return json.loads(json.dumps(d, default={}), object_hook=DynamicObject)


class RelationLabels(Enum):
    def _generate_next_value_(name, start, count, last_values):
        """Sets value of auto() to name of property"""
        return name

    AttachedTo = auto()
    Authenticates = auto()
    ConnectedTo = auto()
    Contains = auto()
    Exposes = auto()
    HasAccessPolicies = auto()
    HasRbac = auto()
    HasRole = auto()
    Manages = auto()
    MemberOf = auto()
    Owns = auto()
    RepresentedBy = auto()
    Trusts = auto()


class Node(BaseModel):
    """Base model for all nodes"""

    _relationships: List["Relationship"] = PrivateAttr(default_factory=list)

    # A. Ignore all extra fields
    # B. Encode DynamicObject by getting the __dict__
    class Config:
        extra = "ignore"
        json_encoders = {DynamicObject: lambda v: v.__dict__}

    def __relationships__(self) -> Iterator["Relationship"]:
        """Override this method to define relationships for resource object."""
        yield []

    @classmethod
    def _labels(cls) -> List[str]:
        """Get the Neo4j labels for subclassed models"""

        label = cls.__qualname__.split(".")[-1]
        if label == "Node":
            return None
        elif label in ["AADObject", "ARMResource"]:
            return [label.upper()]
        else:
            return [label.upper(), cls.__mro__[1].__name__.upper()]

    @property
    def label(self) -> str:
        return qualname_base(self).upper()

    def node(self) -> Dict[str, Any]:
        """Node representation safe for Neo4j"""
        return self.dict(exclude={"properties"})

    def getattr(self, attr: str) -> Any:
        """Returns an object attribute if exists"""
        try:
            return attrgetter(attr)(self)
        except:
            return None


class Relationship(BaseModel):
    """Relationship model"""

    source: str
    source_label: List[str]
    target: str
    target_label: List[str]
    relation: RelationLabels
    properties: Optional[Dict[str, Any]]

    @validator("properties", pre=True, always=True)
    def format_properties(cls, props: Any):
        """Convert DynamicObject to dict"""
        if isinstance(props, dict):
            return props
        elif isinstance(props, DynamicObject):
            return props.__dict__


####--- AAD RELATED MODELS ---###
class AADObject(Node):
    """Base Neo4JModel for AAD objects"""

    id: str
    displayName: str

    def __init__(self, **data: Any) -> None:
        super().__init__(**data)

        # Process owner relations
        # Member is a UUID string that represents an AADObject
        for owner in getattr(self, "owners", []):
            self._relationships.append(
                Relationship(
                    source=self.id,
                    source_label=self._labels(),
                    target=owner,
                    target_label=AADObject._labels(),
                    relation=RelationLabels.Owns,
                )
            )
        # Process member relations
        # Member is a UUID string that represents an AADObject
        for member in getattr(self, "members", []):
            self._relationships.append(
                Relationship(
                    source=member,
                    source_label=AADObject._labels(),
                    target=self.id,
                    target_label=self._labels(),
                    relation=RelationLabels.MemberOf,
                )
            )

        self._relationships.extend(self.__relationships__())

    def node(self) -> Dict[str, Any]:
        """Node representation safe for Neo4j"""
        return self.dict(exclude={"owners", "members"}) | {
            "_relationships": self._relationships
        }


class AADApplication(AADObject):
    appId: str
    appOwnerOrganizationId: Optional[str]
    owners: List[str]
    publisherName: Optional[str]


class AADServicePrincipal(AADObject):
    accountEnabled: bool
    appDisplayName: Optional[str] = ...
    appId: str
    appOwnerOrganizationId: Optional[str] = ...
    owners: List[str]
    publisherName: Optional[str] = ...
    servicePrincipalType: str


class AADGroup(AADObject):
    members: List[str]
    onPremisesSecurityIdentifier: Optional[str] = ...
    organizationId: str
    owners: List[str]
    securityEnabled: bool


class AADRole(AADObject):
    description: str
    deletedDateTime: Optional[str] = ...
    roleTemplateId: str
    members: List[str]


class AADUser(AADObject):
    accountEnabled: bool
    creationType: Optional[str] = ...
    mail: Optional[str] = ...
    mailNickname: Optional[str] = ...
    onPremisesDistinguishedName: Optional[str] = ...
    onPremisesDomainName: Optional[str] = ...
    onPremisesExtensionAttributes: Optional[List[str]] = Field(default_factory=list)
    onPremisesSamAccountName: Optional[str] = ...
    onPremisesSecurityIdentifier: Optional[str] = ...
    onPremisesUserPrincipalName: Optional[str] = ...
    refreshTokensValidFromDateTime: str
    userPrincipalName: str
    userType: str

    @validator("onPremisesExtensionAttributes", pre=True, always=True)
    def exattr_to_values_list(cls, dict_value: dict):
        """Convert extension attributes to list of their values"""
        return list(filter(None, dict_value.values()))


####--- ARM RELATED MODELS ---###
class ARMResource(Node):
    """Base Neo4JModel for ARM resources"""

    # Set this to lowercase value of ARM type for dynamic object creation
    # i.e., microsoft.keyvault/vaults
    __arm_type__: ClassVar[str] = ...
    __xfields__: ClassVar[List[str]] = PrivateAttr(default_factory=list)
    __map_to_resourcegroup__: ClassVar[bool] = True
    __xdict__: Dict[str, Any] = PrivateAttr(default_factory=dict)

    id: str
    location: Optional[str]
    name: Optional[str]
    properties: Optional[DynamicObject]
    tags: Optional[List[str]]

    class Config:
        arbitrary_types_allowed = True

    def __init__(self, **data: Any) -> None:
        super().__init__(**data)

        # Grab the field from the properties and set it as it's own property
        if self.properties and self.__xfields__:
            for field in self.__xfields__:
                try:
                    value = attrgetter(field)(self.properties)
                except:
                    value = None

                field_name = field.split(".")[-1]
                self.__xdict__[field_name] = value

        # Add default resource group relationship
        if self.__map_to_resourcegroup__:
            self._relationships.append(
                Relationship(
                    source=self.resourcegroup,
                    source_label=ResourceGroup._labels(),
                    target=self.id,
                    target_label=self._labels(),
                    relation=RelationLabels.Contains,
                )
            )

        self._relationships.extend(self.__relationships__())

    @property
    def subscription(self) -> str:
        """Get the subscription from the id"""
        return self.id.split("/")[2] if "subscriptions" in self.id else None

    @property
    def resourcegroup(self) -> str:
        """Get the resource group from the id"""
        return self.id.split("/providers")[0] if "providers" in self.id else None

    @validator("tags", pre=True, always=True)
    def convert_to_list(cls, dict_value: dict):
        """Convert tags dictionary to list for neo4j property"""
        if dict_value:
            as_list = []
            [as_list.extend([k, v]) for k, v in dict_value.items()]
            return as_list
        return None

    @validator("properties", pre=True, always=True)
    def props_to_obj(cls, dict_value: str):
        """Convert properties dictionary to dynamic object"""
        return DynamicObject.from_dict(dict_value)

    def node(self) -> Dict[str, Any]:
        """Node representation safe for Neo4j"""
        return (
            self.dict(exclude={"properties"})
            | self.__xdict__
            | {"_relationships": self._relationships}
        )


class Tenant(ARMResource):
    __arm_type__ = "tenant"
    __map_to_resourcegroup__ = False

    tenant_id: str
    tenant_category: str
    country_code: str
    domains: List[str]
    default_domain: str
    tenant_type: str


class Subscription(ARMResource):
    __arm_type__ = "subscription"
    __map_to_resourcegroup__: ClassVar[bool] = False

    tenant_id: str
    subscription_id: str
    state: str
    managed_by_tenants: Optional[List[str]] = Field(default_factory=list)


class ResourceGroup(ARMResource):
    __arm_type__ = "microsoft.resources/resourcegroups"
    __map_to_resourcegroup__: ClassVar[bool] = False


class KeyVault(ARMResource):
    __arm_type__ = "microsoft.keyvault/vaults"
    __xfields__ = [
        "enableSoftDelete",
        "softDeleteRetentionInDays",
        "enableRbacAuthorization",
        "enablePurgeProtection",
        "vaultUri",
    ]

    def __relationships__(self) -> Iterator[Relationship]:
        for policy in self.properties.accessPolicies:
            yield Relationship(
                source=policy.objectId,
                source_label=AADObject._labels(),
                target=self.id,
                target_label=self._labels(),
                relation=RelationLabels.HasAccessPolicies,
                properties=policy.permissions,
            )


class StorageAccount(ARMResource):
    __arm_type__ = "microsoft.storage/storageaccounts"
    __xfields__ = ["accessTier", "creationTime", "supportsHttpsTrafficOnly"]


def get_available_models() -> Dict[str, Node]:
    """Returns models available for Neo4j ingestion"""

    # AAD models need to use qualname or else you get ModelMetaclass back.
    aad_models = {qualname_base(c): c for c in AADObject.__subclasses__()}
    arm_models = {c.__arm_type__: c for c in ARMResource.__subclasses__()}
    return aad_models | arm_models


def get_all_labels() -> List[str]:
    """Returns a list of all labels from all available models"""
    models = AVAILABLE_MODELS
    return sorted(list(set([model._labels()[0] for model in models.values()])))


AVAILABLE_MODELS = get_available_models()