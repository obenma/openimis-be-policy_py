import json
from core.test_helpers import create_test_interactive_user, create_test_officer
from core.models.openimis_graphql_test_case import openIMISGraphQLTestCase, BaseTestContext as DummyContext

from graphql_jwt.shortcuts import get_token

# credits https://docs.graphene-python.org/projects/django/en/latest/testing/
from medical.test_helpers import create_test_item, create_test_service
from insuree.test_helpers import create_test_insuree
from policy.test_helpers import create_test_policy, dts
from contribution.test_helpers import create_test_premium
from product.models import ProductItemOrService
from product.test_helpers import (
    create_test_product,
    create_test_product_service,
    create_test_product_item,
)
from location.test_helpers import create_test_village
from uuid import UUID


class PolicyGraphQLTestCase(openIMISGraphQLTestCase):

    admin_user = None

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.admin_user = create_test_interactive_user(username="testLocationAdmin")
        cls.admin_token = get_token(cls.admin_user, DummyContext(user=cls.admin_user))

        cls.test_village = create_test_village()
        cls.test_ward = cls.test_village.parent
        cls.test_region = cls.test_village.parent.parent.parent
        cls.test_district = cls.test_village.parent.parent
        # Given
        cls.insuree = create_test_insuree(
            custom_props={"current_village": cls.test_village}
        )

        cls.service = create_test_service(
            "A", custom_props={"name": "test_simple_batch"}
        )
        cls.service_2 = create_test_service(
            "a", custom_props={"name": "test_simple_batch"}
        )
        cls.item = create_test_item("A", custom_props={"name": "test_simple_batch"})
        cls.item_2 = create_test_item("a", custom_props={"name": "test_simple_batch"})
        cls.product = create_test_product(
            "BCUL0001",
            custom_props={
                "name": "simplebatch",
                "lump_sum": 10_000,
                "location_id": cls.test_region.id,
            },
        )

        cls.product_service = create_test_product_service(
            cls.product,
            cls.service,
            custom_props={},
        )
        cls.product_item = create_test_product_item(
            cls.product,
            cls.item,
            custom_props={"price_origin": ProductItemOrService.ORIGIN_RELATIVE},
        )
        cls.policy = create_test_policy(cls.product, cls.insuree, link=True)
        cls.premium = create_test_premium(policy_id=cls.policy.id, custom_props={})
        cls.policy_past = create_test_policy(
            cls.product,
            cls.insuree,
            link=True,
            custom_props={
                "enroll_date": dts("2010-01-01"),
                "start_date": dts("2010-01-01"),
                "validity_from": dts("2010-01-01"),
                "effective_date": dts("2010-01-01"),
                "expiry_date": dts("2011-01-01"),
            },
        )
        cls.premium_past = create_test_premium(
            policy_id=cls.policy_past.id, custom_props={"pay_date": dts("2010-01-01")}
        )
        cls.not_insuree = create_test_insuree(
            with_family=False, custom_props={"family": cls.insuree.family}
        )

        cls.test_officer = create_test_officer()

    def test_insuree_policy_query(self):

        response = self.query(
            """
            query {
                policies(first: 10,orderBy: ["-enrollDate"], balanceLte: 100)
                {
                    totalCount
                    pageInfo { hasNextPage, hasPreviousPage, startCursor, endCursor}
                    edges
                    {
                        node
                        {
                            uuid,product{id,code,name,location{id}},officer{id,uuid,code,lastName,otherNames},family{id,uuid,headInsuree{id chfId uuid lastName otherNames},location{id,uuid,code,name,type,parent{id,uuid,code,name,type,parent{id,uuid,code,name,type,parent{id,uuid,code,name,type}}}}},enrollDate,effectiveDate,startDate,expiryDate,stage,status,value,sumPremiums,validityFrom,validityTo
                        }
                    }
                }
            }
            """,
            headers={"HTTP_AUTHORIZATION": f"Bearer {self.admin_token}"},
        )

        json.loads(response.content)

        # This validates the status code and if you get errors
        self.assertResponseNoErrors(response)

        # Add some more asserts if you like
        ...

    def test_query_not_insured_family_member(self):
        response = self.query(
            """

            query policiesByInsuree($chfid: String!) {
                policiesByInsuree(chfId:$chfid)
                {
                    totalCount
                    pageInfo { hasNextPage, hasPreviousPage, startCursor, endCursor}
                    edges
                    {
                        node
                        {
                            policyUuid,productCode,productName,officerCode,officerName,enrollDate,effectiveDate,startDate,expiryDate,status,policyValue,balance,ded,dedInPatient,dedOutPatient,ceiling,ceilingInPatient,ceilingOutPatient
                        }
                    }
                }
            }
            """,
            headers={"HTTP_AUTHORIZATION": f"Bearer {self.admin_token}"},
            variables={
                "chfid": self.not_insuree.chf_id,
                "targetDate": self.policy.effective_date.strftime("%Y-%m-%d"),
            },
        )

        content = json.loads(response.content)

        # This validates the status code and if you get errors
        self.assertResponseNoErrors(response)
        self.assertEqual(content["data"]["policiesByInsuree"]["totalCount"], 0)

    def test_query_with_variables(self):
        response = self.query(
            """

            query policiesByInsuree($chfid: String!) {
                policiesByInsuree(chfId:$chfid)
                {
                    totalCount
                    pageInfo { hasNextPage, hasPreviousPage, startCursor, endCursor}
                    edges
                    {
                        node
                        {
                            policyUuid,productCode,productName,officerCode,officerName,enrollDate,effectiveDate,startDate,expiryDate,status,policyValue,balance,ded,dedInPatient,dedOutPatient,ceiling,ceilingInPatient,ceilingOutPatient
                        }
                    }
                }
            }
            """,
            headers={"HTTP_AUTHORIZATION": f"Bearer {self.admin_token}"},
            variables={
                "chfid": self.insuree.chf_id,
                "targetDate": self.policy.effective_date.strftime("%Y-%m-%d"),
            },
        )

        content = json.loads(response.content)

        # This validates the status code and if you get errors
        self.assertResponseNoErrors(response)
        self.assertEqual(content["data"]["policiesByInsuree"]["totalCount"], 2)

    def test_query_with_variables_2(self):
        response = self.query(
            """

            query policiesByInsuree($chfid: String!, $activeOrLastExpiredOnly: Boolean!) {
                policiesByInsuree(chfId:$chfid, activeOrLastExpiredOnly:$activeOrLastExpiredOnly)
                {
                    totalCount
                    pageInfo { hasNextPage, hasPreviousPage, startCursor, endCursor}
                    edges
                    {
                        node
                        {
                            policyUuid,productCode,productName,officerCode,officerName,enrollDate,effectiveDate,startDate,expiryDate,status,policyValue,balance,ded,dedInPatient,dedOutPatient,ceiling,ceilingInPatient,ceilingOutPatient
                        }
                    }
                }
            }
            """,
            headers={"HTTP_AUTHORIZATION": f"Bearer {self.admin_token}"},
            variables={"chfid": self.insuree.chf_id, "activeOrLastExpiredOnly": True},
        )

        content = json.loads(response.content)

        # This validates the status code and if you get errors
        self.assertResponseNoErrors(response)
        self.assertEqual(content["data"]["policiesByInsuree"]["totalCount"], 1)
        self.assertEqual(
            UUID(
                content["data"]["policiesByInsuree"]["edges"][0]["node"]["policyUuid"]
            ),
            UUID(self.policy.uuid),
        )

    def test_query_with_variables_3(self):
        response = self.query(
            """

            query policiesByInsuree($chfid: String!, $targetDate:  Date! ) {
                policiesByInsuree(chfId:$chfid ,targetDate: $targetDate)
                {
                    totalCount
                    pageInfo { hasNextPage, hasPreviousPage, startCursor, endCursor}
                    edges
                    {
                        node
                        {
                            policyUuid,productCode,productName,officerCode,officerName,enrollDate,effectiveDate,startDate,expiryDate,status,policyValue,balance,ded,dedInPatient,dedOutPatient,ceiling,ceilingInPatient,ceilingOutPatient
                        }
                    }
                }
            }
            """,
            headers={"HTTP_AUTHORIZATION": f"Bearer {self.admin_token}"},
            variables={
                "chfid": self.insuree.chf_id,
                "targetDate": self.policy.effective_date.strftime("%Y-%m-%d"),
            },
        )

        content = json.loads(response.content)

        # This validates the status code and if you get errors
        self.assertResponseNoErrors(response)
        self.assertEqual(content["data"]["policiesByInsuree"]["totalCount"], 1)
        self.assertEqual(
            UUID(
                content["data"]["policiesByInsuree"]["edges"][0]["node"]["policyUuid"]
            ),
            UUID(self.policy.uuid),
        )

    def test_family_query_with_variables(self):
        response = self.query(
            """
            query policiesByFamily($familyUuid: String!, $targetDate:  Date! ) {
                policiesByFamily(orderBy: "expiryDate",activeOrLastExpiredOnly: true,familyUuid:$familyUuid ,targetDate: $targetDate,first: 5)
                {
                    totalCount
                    pageInfo { hasNextPage, hasPreviousPage, startCursor, endCursor}
                    edges
                    {
                        node
                        {
                            policyUuid,productCode,productName,officerCode,officerName,enrollDate,effectiveDate,startDate,expiryDate,status,policyValue,balance,ded,dedInPatient,dedOutPatient,ceiling,ceilingInPatient,ceilingOutPatient
                        }
                    }
                }
            }
            """,
            headers={"HTTP_AUTHORIZATION": f"Bearer {self.admin_token}"},
            variables={
                "familyUuid": str(self.insuree.family.uuid),
                "targetDate": self.policy.effective_date.strftime("%Y-%m-%d"),
            },
        )

        content = json.loads(response.content)

        # This validates the status code and if you get errors
        self.assertResponseNoErrors(response)
        self.assertEqual(content["data"]["policiesByFamily"]["totalCount"], 1)
        self.assertEqual(
            UUID(content["data"]["policiesByFamily"]["edges"][0]["node"]["policyUuid"]),
            UUID(self.policy.uuid),
        )

    def test_insuree_policy_service_query(self):

        response = self.query(
            f"""
{{
  policyServiceEligibilityByInsuree(chfId:"{self.insuree.chf_id}", serviceCode:"{self.service.code}")
  {{
    minDateService, serviceLeft, isServiceOk
  }}
}}
            """,
            headers={"HTTP_AUTHORIZATION": f"Bearer {self.admin_token}"},
        )

        json.loads(response.content)

        # This validates the status code and if you get errors
        self.assertResponseNoErrors(response)

    def test_insuree_policy_item_query(self):

        # Add some more asserts if you like
        response = self.query(
            f"""
{{
  policyItemEligibilityByInsuree(chfId:"{self.insuree.chf_id}",itemCode:"{self.item.code}")
  {{
     minDateItem,itemLeft,isItemOk
  }}
}}
            """,
            headers={"HTTP_AUTHORIZATION": f"Bearer {self.admin_token}"},
        )

        json.loads(response.content)

        # This validates the status code and if you get errors
        self.assertResponseNoErrors(response)

    def test_insuree_policy_wrong_service_query(self):
        response = self.query(
            f"""
{{
  policyItemEligibilityByInsuree(chfId:"{self.insuree.chf_id}",itemCode:"IDONOTEXIST")
  {{
     minDateItem,itemLeft,isItemOk
  }}
}}
            """,
            headers={"HTTP_AUTHORIZATION": f"Bearer {self.admin_token}"},
        )

        # This validates the status code and if you get errors
        self.assertResponseHasErrors(response)

        # Add some more asserts if you like

    def test_mutation_simple(self):
        muuid = "203327cd-501e-41e1-a026-ed742e360081"
        self.query(
            f"""
    mutation {{
      createPolicy(
        input: {{
          clientMutationId: "{muuid}"
          clientMutationLabel: "Create policy ttttt eeeee (123123123) - 2024-06-01 : 2025-05-31"

          enrollDate: "2024-04-07"
            startDate: "2024-06-01"
            expiryDate: "2025-05-31"
            value: "10000.00"
            productId: {self.product.id}
            familyId: {self.insuree.family.id}
            officerId: {self.test_officer.id}
                    }}
        ) {{
            clientMutationId
            internalId
        }}
    }}
            """,
            headers={"HTTP_AUTHORIZATION": f"Bearer {self.admin_token}"},
            variables={"chfid": self.insuree.chf_id, "activeOrLastExpiredOnly": True},
        )
        self.get_mutation_result(muuid, self.admin_token)

    def test_insuree_policy_value_query(self):

        response = self.query(
            f"""
                {{
                policyValues(
                    stage: "R",
                    enrollDate: "2019-09-26T00:00:00",
                    productId: {self.product.id},
                    familyId: {self.insuree.family.id}
                ){{
                    policy{{startDate expiryDate value}},warnings
                }}
                }} """,
            headers={"HTTP_AUTHORIZATION": f"Bearer {self.admin_token}"},
        )

        json.loads(response.content)

        # This validates the status code and if you get errors
        self.assertResponseNoErrors(response)
